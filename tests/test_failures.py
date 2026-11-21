"""Отказы внешних сервисов: OpenRouter (HTTP/сеть/JSON) и Telegram."""
from datetime import timedelta

import httpx
import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from conftest import CHAT_A, USER_ALICE, build_harness, llm_response, summary_payload
from service import PERIOD_24H, ManualRequest
from summary_generator import SummaryGenerationError


def req():
    return ManualRequest(chat_id=CHAT_A, user_id=USER_ALICE, period=PERIOD_24H, reply_to=900)


def seed(h):
    h.add(CHAT_A, 1, 120, "первое")
    h.add(CHAT_A, 2, 60, "второе")


def error(status, message="ошибка", headers=None):
    return httpx.Response(status, json={"error": {"code": status, "message": message}}, headers=headers or {})


async def test_openrouter_5xx_retries_with_backoff_then_fails(h):
    """Сценарий 8: отказ OpenRouter — ограниченные повторы, лимит не расходуется, сводка не публикуется."""
    seed(h)
    h.llm.push(*[error(502)] * 10)
    assert await h.service.run_manual(req()) is False
    assert len(h.llm.requests) == 4  # 1 + LLM_MAX_RETRIES=3
    assert len(h.sleeps) == 3
    assert h.sleeps[0] <= h.sleeps[1] * 2 and h.sleeps[1] <= h.sleeps[2] * 2  # экспоненциальный рост
    assert all(s <= 60 for s in h.sleeps)
    assert "Лимит запросов не израсходован" in h.bot.texts()[-1]
    assert "Сводка за" not in "".join(h.bot.texts())
    assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"
    assert h.db.last_summary(CHAT_A).status == "failed"


async def test_openrouter_429_respects_retry_after_and_recovers(h):
    seed(h)
    h.llm.push(error(429, "rate limited", {"Retry-After": "7"}), httpx.ConnectError("boom"), None)
    h.llm.queue[-1] = lambda body: llm_response(summary_payload([1, 2]))
    assert await h.service.run_manual(req()) is True
    assert h.sleeps[0] == 7
    assert len(h.llm.requests) == 3


async def test_openrouter_auth_error_is_not_retried(h):
    seed(h)
    h.llm.push(error(401, "invalid key"))
    assert await h.service.run_manual(req()) is False
    assert len(h.llm.requests) == 1


async def test_openrouter_error_inside_http_200_is_retried(h):
    seed(h)
    h.llm.push(httpx.Response(200, json={"error": {"code": 502, "message": "provider down"}}),
               httpx.Response(200, json={"choices": []}))
    assert await h.service.run_manual(req()) is True
    assert len(h.llm.requests) == 3


async def test_truncated_output_is_reported_without_retry(h):
    seed(h)
    h.llm.push(llm_response('{"topics": [', finish_reason="length"))
    assert await h.service.run_manual(req()) is False
    assert len(h.llm.requests) == 1
    assert "LLM_MAX_OUTPUT_TOKENS" in h.db.last_summary(CHAT_A).error


async def test_invalid_json_is_retried_once_then_succeeds(h):
    """Сценарий 9а: некорректный JSON — повторный запрос, затем успех."""
    seed(h)
    h.llm.push(llm_response("это не JSON"))
    assert await h.service.run_manual(req()) is True
    assert len(h.llm.requests) == 2


async def test_invalid_json_twice_fails_without_publishing(h):
    """Сценарий 9б: модель стабильно отдаёт мусор — ничего не публикуется, лимит не тронут."""
    seed(h)
    h.llm.push(llm_response('{"topics": "не список"}'), llm_response('{"foo": 1}'))
    assert await h.service.run_manual(req()) is False
    assert len(h.llm.requests) == 2
    assert all("Сводка за" not in t for t in h.bot.texts())
    assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"
    assert "некорректный результат" in h.db.last_summary(CHAT_A).error


async def test_generator_never_publishes_raw_llm_text(h):
    seed(h)
    h.llm.push(llm_response("Сырой текст <b>с разметкой</b>"), llm_response("снова сырой текст"))
    await h.service.run_manual(req())
    assert all("Сырой текст" not in t for t in h.bot.texts())


async def test_telegram_publish_error_does_not_consume_limit(h):
    """Сценарий 10а: ошибка публикации — лимит не расходуется."""
    seed(h)
    h.bot.errors = [Forbidden("bot was kicked")]
    assert await h.service.run_manual(req()) is False
    assert h.service.begin_manual(CHAT_A, USER_ALICE).status == "ok"
    assert "ошибка отправки" in h.db.last_summary(CHAT_A).error


async def test_telegram_transient_errors_are_retried(h):
    """Сценарий 10б: RetryAfter и тайм-ауты — повторная отправка."""
    seed(h)
    h.bot.errors = [RetryAfter(timedelta(seconds=3)), TimedOut()]
    assert await h.service.run_manual(req()) is True
    assert h.sleeps[0] == 4  # retry_after + 1
    assert len(h.bot.sent) == 1


async def test_telegram_html_rejection_falls_back_to_plain_text(h):
    seed(h)
    h.bot.errors = [BadRequest("Can't parse entities: unexpected end tag")]
    assert await h.service.run_manual(req()) is True
    assert h.bot.sent[0]["parse_mode"] is None
    assert "<b>" not in h.bot.sent[0]["text"]


async def test_scheduled_publish_failure_is_retried_later_without_regeneration(tmp_path):
    """Сценарий 10в: плановая сводка не отправилась — повтор позже, без повторного вызова LLM."""
    from conftest import Clock
    from datetime import datetime, timezone

    clock = Clock(datetime(2026, 11, 21, 19, 0, 30, tzinfo=timezone.utc).timestamp())  # 22:00:30 МСК
    h = build_harness(tmp_path, clock=clock, WEEKLY_SUMMARY_ENABLED="false")
    h.db.set_meta("installed_at", "0")
    try:
        seed(h)
        h.bot.errors = [TimedOut()] * 5  # все попытки внутри одной публикации неуспешны
        assert await h.scheduler.tick() == 2  # обе группы; CHAT_B пуста → skipped
        run = h.db.get_scheduled_run(CHAT_A, "daily", "2026-11-21T22:00:00+03:00")
        assert run["status"] == "failed" and run["next_attempt_at"] > clock()
        assert len(h.llm.requests) == 1

        assert await h.scheduler.tick() == 0  # до next_attempt_at повторов нет
        clock.advance(301)
        assert await h.scheduler.tick() == 1
        run = h.db.get_scheduled_run(CHAT_A, "daily", "2026-11-21T22:00:00+03:00")
        assert run["status"] == "done"
        assert len(h.llm.requests) == 1  # сводка не генерировалась заново
        assert sum("Ежедневная сводка" in t for t in h.bot.texts()) == 1
    finally:
        await h.close()


async def test_generation_error_type():
    with pytest.raises(SummaryGenerationError):
        raise SummaryGenerationError("x")
