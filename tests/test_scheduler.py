"""Расписание: часовые пояса, слоты, пропущенные запуски, отсутствие дублей,
перезапуск во время задачи, сохранение истории после ежедневной сводки."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from conftest import CHAT_A, CHAT_B, Clock, build_harness
from scheduler import latest_daily_slot, latest_weekly_slot, next_daily_slot, next_weekly_slot

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def ts(*args) -> float:
    return utc(*args).timestamp()


# ------------------------------------------------------------ вычисление слотов

def test_daily_slot_respects_timezone():
    """Сценарий 13: 22:00 по Москве = 19:00 UTC."""
    at = time(22, 0)
    assert latest_daily_slot(utc(2026, 11, 21, 18, 59), MSK, at) == datetime(2026, 11, 20, 22, 0, tzinfo=MSK)
    assert latest_daily_slot(utc(2026, 11, 21, 19, 0), MSK, at) == datetime(2026, 11, 21, 22, 0, tzinfo=MSK)
    assert next_daily_slot(utc(2026, 11, 21, 19, 0), MSK, at).astimezone(timezone.utc) == utc(2026, 11, 22, 19, 0)


def test_weekly_slot_independent_day_and_time():
    at = time(23, 0)
    sunday = 6
    # суббота 21.11.2026 → последний слот — воскресенье 15.11 23:00 МСК
    assert latest_weekly_slot(utc(2026, 11, 21, 12, 0), MSK, sunday, at) == datetime(2026, 11, 15, 23, 0, tzinfo=MSK)
    # воскресенье 22.11 22:59 МСК — ещё прошлый слот; 23:00 — новый
    assert latest_weekly_slot(utc(2026, 11, 22, 19, 59), MSK, sunday, at).day == 15
    assert latest_weekly_slot(utc(2026, 11, 22, 20, 0), MSK, sunday, at).day == 22
    assert next_weekly_slot(utc(2026, 11, 22, 20, 0), MSK, sunday, at).day == 29


def test_slots_across_dst_change():
    """Переход на зимнее время в Берлине (25.10.2026): 22:00 местного остаётся 22:00."""
    at = time(22, 0)
    before = latest_daily_slot(utc(2026, 10, 24, 21, 0), BERLIN, at)
    after = latest_daily_slot(utc(2026, 10, 25, 22, 0), BERLIN, at)
    assert before.astimezone(timezone.utc) == utc(2026, 10, 24, 20, 0)  # CEST, UTC+2
    assert after.astimezone(timezone.utc) == utc(2026, 10, 25, 21, 0)   # CET, UTC+1
    # окно «последние 24 часа» — абсолютные 24 часа, а не «календарные сутки»
    assert after.timestamp() - before.timestamp() == 25 * 3600


# ------------------------------------------------------------- выполнение задач

def make(tmp_path, now: float, **env):
    h = build_harness(tmp_path, clock=Clock(now), **env)
    h.db.set_meta("installed_at", "0")
    return h


async def test_daily_summary_runs_once_per_period(tmp_path):
    h = make(tmp_path, ts(2026, 11, 21, 19, 0, 10), WEEKLY_SUMMARY_ENABLED="false")
    try:
        h.add(CHAT_A, 1, 3600, "в окне")
        h.add(CHAT_A, 2, 1800, "тоже в окне")
        h.add(CHAT_A, 3, 0, "после слота — не входит")
        assert await h.scheduler.tick() == 2
        assert await h.scheduler.tick() == 0
        h.clock.advance(3600)
        assert await h.scheduler.tick() == 0
        daily = [t for t in h.bot.texts() if "Ежедневная сводка" in t]
        assert len(daily) == 1
        assert '"id": 3' not in h.llm.user_contents()[0]
        assert h.db.get_scheduled_run(CHAT_B, "daily", "2026-11-21T22:00:00+03:00")["status"] == "skipped"
    finally:
        await h.close()


async def test_history_is_kept_after_daily_and_used_by_weekly(tmp_path):
    """Сценарий 12: сообщения не удаляются после ежедневной сводки и входят в еженедельную."""
    # воскресенье 22.11.2026, 22:00 МСК — ежедневная; 23:00 МСК — еженедельная
    h = make(tmp_path, ts(2026, 11, 22, 19, 0, 5))
    try:
        h.add(CHAT_A, 1, 5 * 86400, "пять дней назад")
        h.add(CHAT_A, 2, 3600, "час назад")
        h.add(CHAT_A, 3, 600, "десять минут назад")
        await h.scheduler.tick()
        assert h.db.count_messages(CHAT_A) == 3

        h.clock.t = ts(2026, 11, 22, 20, 0, 5)
        await h.scheduler.tick()
        weekly_request = h.llm.user_contents()[-1]
        for mid in (1, 2, 3):
            assert f'"id": {mid}' in weekly_request
        assert h.db.count_messages(CHAT_A) == 3
        assert any("Еженедельная сводка за 7 дней" in t for t in h.bot.texts())
    finally:
        await h.close()


async def test_missed_run_is_caught_up_after_downtime(tmp_path):
    """Сервис был выключен в 22:00 и запущен в 03:00 — сводка за тот же период публикуется."""
    h = make(tmp_path, ts(2026, 11, 22, 0, 0), WEEKLY_SUMMARY_ENABLED="false")
    try:
        h.add(CHAT_A, 1, 6 * 3600)
        h.add(CHAT_A, 2, 5 * 3600 + 1)
        assert await h.scheduler.tick() == 2
        run = h.db.get_scheduled_run(CHAT_A, "daily", "2026-11-21T22:00:00+03:00")
        assert run["status"] == "done"
        assert run["window_end"] == int(ts(2026, 11, 21, 19, 0))
        assert run["window_start"] == int(ts(2026, 11, 20, 19, 0))
    finally:
        await h.close()


async def test_stale_missed_run_is_not_published(tmp_path):
    h = make(tmp_path, ts(2026, 11, 22, 8, 0), WEEKLY_SUMMARY_ENABLED="false",
             MISSED_SUMMARY_MAX_DELAY_HOURS=12)  # 13 часов после слота
    try:
        h.add(CHAT_A, 1, 20 * 3600)
        h.add(CHAT_A, 2, 19 * 3600)
        assert await h.scheduler.tick() == 0
        assert h.llm.requests == []
    finally:
        await h.close()


async def test_no_catch_up_for_slots_before_first_install(tmp_path):
    h = build_harness(tmp_path, clock=Clock(ts(2026, 11, 21, 19, 30)), WEEKLY_SUMMARY_ENABLED="false")
    try:  # installed_at = сейчас (19:30 UTC), слот 19:00 UTC — до установки
        h.add(CHAT_A, 1, 600)
        h.add(CHAT_A, 2, 300)
        assert await h.scheduler.tick() == 0
    finally:
        await h.close()


async def test_restart_during_generation_reruns_without_duplicate(tmp_path):
    """Сценарий 11а: процесс упал во время генерации — после перезапуска задача
    выполняется снова и публикуется ровно один раз."""
    clock = Clock(ts(2026, 11, 21, 19, 0, 30))
    h1 = make(tmp_path, clock.t, WEEKLY_SUMMARY_ENABLED="false", TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A))
    h1.add(CHAT_A, 1, 600)
    h1.add(CHAT_A, 2, 300)
    period = "2026-11-21T22:00:00+03:00"
    run = h1.db.claim_scheduled_run(CHAT_A, "daily", period, int(clock.t) - 86400 - 30, int(clock.t) - 30,
                                    int(clock.t), 3600, 5)
    sid = h1.db.create_summary(CHAT_A, "daily", run.window_start, run.window_end, int(clock.t), period_key=period)
    h1.db.set_run_summary(CHAT_A, "daily", period, sid, int(clock.t))
    await h1.close()  # «падение»: задача осталась в статусе running, сводка — generating

    clock.advance(60)
    h2 = build_harness(tmp_path, clock=Clock(clock.t), WEEKLY_SUMMARY_ENABLED="false",
                       TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A))
    try:
        assert h2.db.get_summary(sid).status == "failed"  # прерванная генерация помечена
        assert await h2.scheduler.tick() == 1
        assert await h2.scheduler.tick() == 0
        assert h2.db.get_scheduled_run(CHAT_A, "daily", period)["status"] == "done"
        assert h2.db.get_scheduled_run(CHAT_A, "daily", period)["attempts"] == 2
        assert sum("Ежедневная сводка" in t for t in h2.bot.texts()) == 1
    finally:
        await h2.close()


async def test_restart_during_sending_resumes_remaining_parts(tmp_path):
    """Сценарий 11б: упали после отправки части сообщений — досылаются только
    оставшиеся части, без повторной генерации."""
    clock = Clock(ts(2026, 11, 21, 19, 0, 30))
    h1 = make(tmp_path, clock.t, WEEKLY_SUMMARY_ENABLED="false", TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A))
    period = "2026-11-21T22:00:00+03:00"
    h1.db.claim_scheduled_run(CHAT_A, "daily", period, 0, int(clock.t), int(clock.t), 3600, 5)
    sid = h1.db.create_summary(CHAT_A, "daily", 0, int(clock.t), int(clock.t), period_key=period)
    h1.db.set_run_summary(CHAT_A, "daily", period, sid, int(clock.t))
    h1.db.update_summary(sid, int(clock.t), status="ready", parts=["часть 1", "часть 2", "часть 3"], parts_sent=1)
    await h1.close()

    h2 = build_harness(tmp_path, clock=Clock(clock.t + 30), WEEKLY_SUMMARY_ENABLED="false",
                       TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A))
    try:
        assert await h2.scheduler.tick() == 1
        assert h2.bot.texts() == ["часть 2", "часть 3"]
        assert h2.llm.requests == []
        assert h2.db.get_summary(sid).status == "sent"
    finally:
        await h2.close()


async def test_scheduled_task_gives_up_after_max_attempts(tmp_path):
    h = make(tmp_path, ts(2026, 11, 21, 19, 0, 30), WEEKLY_SUMMARY_ENABLED="false",
             TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A), SCHEDULED_SUMMARY_MAX_ATTEMPTS=2, LLM_MAX_RETRIES=0)
    try:
        import httpx

        h.add(CHAT_A, 1, 600)
        h.add(CHAT_A, 2, 300)
        h.llm.push(*[httpx.Response(503)] * 10)
        await h.scheduler.tick()
        h.clock.advance(400)
        await h.scheduler.tick()
        h.clock.advance(4000)
        assert await h.scheduler.tick() == 0
        assert h.db.get_scheduled_run(CHAT_A, "daily", "2026-11-21T22:00:00+03:00")["status"] == "gave_up"
        assert len(h.llm.requests) == 2
    finally:
        await h.close()


async def test_empty_auto_summary_notice_is_configurable(tmp_path):
    h = make(tmp_path, ts(2026, 11, 21, 19, 0, 30), WEEKLY_SUMMARY_ENABLED="false",
             TELEGRAM_GROUP_CHAT_IDS=str(CHAT_A), POST_EMPTY_AUTO_SUMMARY="true", MIN_MESSAGES_FOR_SUMMARY=3)
    try:
        h.add(CHAT_A, 1, 600)
        await h.scheduler.tick()
        assert h.llm.requests == []
        assert "недостаточно для сводки" in h.bot.texts()[0]
    finally:
        await h.close()
