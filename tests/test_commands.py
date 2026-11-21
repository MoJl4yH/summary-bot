"""Команды: ограничение частоты, параллельные /summary, права доступа, /status, /chatid."""
import asyncio

from conftest import (
    CHAT_A, CHAT_B, CHAT_FOREIGN, USER_ALICE, USER_BOB, USER_MALLORY, build_harness, command_update, context,
)


def seed(h, chat_id=CHAT_A, n=3):
    for i in range(1, n + 1):
        h.add(chat_id, i, 600 - i * 10, f"сообщение {i}")


async def summary(h, chat_id=CHAT_A, user_id=USER_ALICE, *args, chat_type="supergroup"):
    update = command_update(chat_id, user_id, bot=h.bot, chat_type=chat_type)
    await h.app.cmd_summary(update, context(*args))
    await h.app.wait_background(timeout=5)
    return update


async def test_rate_limit_one_manual_summary_per_hour_per_group(h):
    """Сценарий 6: лимит общий для группы, повтор не обращается к OpenRouter, сообщается время."""
    seed(h)
    await summary(h, user_id=USER_ALICE)
    assert len(h.llm.requests) == 1

    h.clock.advance(600)
    sent_before = len(h.bot.sent)
    await summary(h, user_id=USER_BOB)  # другой участник — тот же лимит группы
    assert len(h.llm.requests) == 1
    reply = h.bot.sent[sent_before]["text"]
    assert "не чаще одного раза" in reply
    assert "22.11.2026 00:00" in reply and "через 50 мин" in reply

    # другая группа имеет собственный лимит
    seed(h, CHAT_B)
    await summary(h, CHAT_B, USER_BOB)
    assert len(h.llm.requests) == 2

    h.clock.advance(3000)  # прошёл час с успешной сводки
    await summary(h, user_id=USER_BOB)
    assert len(h.llm.requests) == 3


async def test_rate_limit_survives_restart(tmp_path):
    h1 = build_harness(tmp_path)
    seed(h1)
    await summary(h1)
    assert len(h1.llm.requests) == 1
    await h1.close()

    h2 = build_harness(tmp_path, clock=h1.clock)  # новый процесс, та же БД
    try:
        h2.clock.advance(60)
        assert h2.service.begin_manual(CHAT_A, USER_ALICE).status == "cooldown"
        await summary(h2)
        assert h2.llm.requests == []
    finally:
        await h2.close()


async def test_concurrent_summary_commands_start_single_generation(h):
    """Сценарий 7: одновременные /summary — одна генерация, один вызов LLM."""
    seed(h)
    gate = asyncio.Event()

    async def slow_complete(messages, *, json_mode=True, _orig=h.client.complete):
        await gate.wait()
        return await _orig(messages, json_mode=json_mode)

    h.client.complete = slow_complete
    updates = [command_update(CHAT_A, uid, bot=h.bot, message_id=900 + uid) for uid in (1, 2, 3, 4, 5)]
    await asyncio.gather(*(h.app.cmd_summary(u, context()) for u in updates))
    replies = [m["text"] for m in h.bot.sent]
    assert sum("Формирую сводку" in r for r in replies) == 1
    assert sum("уже формируется" in r for r in replies) == 1  # остальные ответы подавлены анти-флудом
    gate.set()
    await h.app.wait_background(timeout=5)
    assert len(h.llm.requests) == 1
    assert sum("Сводка за 24 часа" in m["text"] for m in h.bot.sent) == 1


async def test_automatic_summaries_do_not_consume_manual_limit(h):
    seed(h)
    h.clock.t = _ts(2026, 11, 22, 19, 0, 5)  # 22:00:05 МСК — наступил ежедневный слот
    assert await h.scheduler.tick() >= 1
    assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"


async def test_unauthorized_group_is_refused(h):
    """Сценарий 15а: неразрешённая группа — отказ, LLM не вызывается."""
    seed(h, CHAT_FOREIGN)
    await summary(h, CHAT_FOREIGN)
    assert h.llm.requests == []
    assert "не подключена" in h.bot.texts()[-1]

    await h.app.cmd_status(command_update(CHAT_FOREIGN, USER_ALICE, "/status", bot=h.bot), context())
    assert "Состояние сервиса" not in h.bot.texts()[-1]

    await summary(h, 555, USER_ALICE, chat_type="private")
    assert h.llm.requests == []


async def test_unauthorized_user_is_refused(tmp_path):
    """Сценарий 15б: пользователь вне ALLOWED_USER_IDS не может запустить генерацию."""
    h = build_harness(tmp_path, ALLOWED_USER_IDS=f"{USER_ALICE}")
    try:
        seed(h)
        await summary(h, user_id=USER_MALLORY)
        assert h.llm.requests == []
        assert "нет прав" in h.bot.texts()[-1]
        assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"  # лимит не тронут
        h.db.finish_manual(CHAT_A, success=False, now=int(h.clock()))
        await summary(h, user_id=USER_ALICE)
        assert len(h.llm.requests) == 1
    finally:
        await h.close()


async def test_invalid_period_argument(h):
    seed(h)
    await summary(h, CHAT_A, USER_ALICE, "1y")
    assert h.llm.requests == []
    assert "Формат" in h.bot.texts()[-1]
    assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"


async def test_summary_7d_command(h):
    seed(h)
    h.add(CHAT_A, 50, 5 * 86400, "пять дней назад")
    await summary(h, CHAT_A, USER_ALICE, "7d")
    assert '"id": 50' in h.llm.user_contents()[0]
    assert "Сводка за 7 дней" in h.bot.texts()[-1]


async def test_status_and_chatid_and_help(h):
    seed(h)
    await summary(h)
    await h.app.cmd_status(command_update(CHAT_A, USER_BOB, "/status", bot=h.bot), context())
    status = h.bot.texts()[-1]
    assert "Состояние сервиса" in status and "опубликована" in status
    assert "Ручная сводка (/summary): доступна с" in status
    assert "$0.0010" in status

    await h.app.cmd_chatid(command_update(CHAT_FOREIGN, USER_BOB, "/chatid", bot=h.bot), context())
    assert str(CHAT_FOREIGN) in h.bot.texts()[-1] and "не подключена" in h.bot.texts()[-1]

    await h.app.cmd_help(command_update(CHAT_A, USER_BOB, "/help", bot=h.bot), context())
    help_text = h.bot.texts()[-1]
    assert "/summary 7d" in help_text
    assert "каждый день в 22:00" in help_text and "по воскресеньям в 23:00" in help_text
    assert "любой участник группы" in help_text and "60 мин." in help_text


def _ts(*args):
    from datetime import datetime, timezone

    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


async def test_chatid_shows_user_id_and_allowlist_status(tmp_path):
    h = build_harness(tmp_path, ALLOWED_USER_IDS=f"{USER_ALICE}")
    try:
        await h.app.cmd_chatid(command_update(CHAT_A, USER_ALICE, "/chatid", bot=h.bot), context())
        assert f"Ваш user_id: <code>{USER_ALICE}</code> — есть в ALLOWED_USER_IDS" in h.bot.texts()[-1]
        await h.app.cmd_chatid(command_update(CHAT_A, USER_MALLORY, "/chatid", bot=h.bot, message_id=901), context())
        assert f"<code>{USER_MALLORY}</code> — нет в ALLOWED_USER_IDS" in h.bot.texts()[-1]
        await h.app.cmd_chatid(command_update(CHAT_A, 1087968824, "/chatid", bot=h.bot, message_id=902), context())
        assert "ваш личный ID скрыт" in h.bot.texts()[-1]
    finally:
        await h.close()


async def test_status_restricted_to_allowed_users(tmp_path):
    h = build_harness(tmp_path, ALLOWED_USER_IDS=f"{USER_ALICE}")
    try:
        await h.app.cmd_status(command_update(CHAT_A, USER_MALLORY, "/status", bot=h.bot), context())
        assert "нет прав" in h.bot.texts()[-1] and "Состояние сервиса" not in h.bot.texts()[-1]
        await h.app.cmd_status(command_update(CHAT_A, USER_ALICE, "/status", bot=h.bot, message_id=901), context())
        assert "Состояние сервиса" in h.bot.texts()[-1]
    finally:
        await h.close()
