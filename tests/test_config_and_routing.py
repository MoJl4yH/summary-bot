"""Конфигурация, маскирование секретов, маршрутизация обновлений через настоящий PTB Application."""
import logging
from datetime import datetime, time, timezone

import pytest
from telegram import Update, User
from telegram.ext import Application

from config import ConfigError, load_settings
from conftest import CHAT_A, T0, build_harness, make_env, tg_message
from logging_setup import SecretMaskingFormatter, mask_secrets


def test_target_config_names(tmp_path):
    s = load_settings(make_env(
        tmp_path, DAILY_SUMMARY_ENABLED="true", DAILY_SUMMARY_TIME="21:30", WEEKLY_SUMMARY_ENABLED="false",
        WEEKLY_SUMMARY_DAY="friday", WEEKLY_SUMMARY_TIME="18:00", MANUAL_SUMMARY_COOLDOWN_SECONDS="1800",
        MESSAGE_RETENTION_DAYS="45", ALLOWED_USER_IDS="1, 2;3",
    ))
    assert s.daily_time == time(21, 30) and s.weekly_day == 4 and s.weekly_time == time(18, 0)
    assert not s.weekly_enabled
    assert s.manual_cooldown_seconds == 1800 and s.message_retention_days == 45
    assert s.allowed_user_ids == frozenset({1, 2, 3})
    assert s.group_chat_ids == frozenset({-1001000000001, -1001000000002})


def test_legacy_summary_time_is_supported(tmp_path):
    env = make_env(tmp_path, SUMMARY_TIME="20:15")
    del env["DAILY_SUMMARY_TIME"]
    assert load_settings(env).daily_time == time(20, 15)


def test_invalid_config_lists_all_errors(tmp_path):
    env = make_env(tmp_path, DAILY_SUMMARY_TIME="25:00", WEEKLY_SUMMARY_DAY="someday", TIMEZONE="Mars/Base",
                   MESSAGE_RETENTION_DAYS="3", TELEGRAM_GROUP_CHAT_IDS="abc")
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    text = "\n".join(exc.value.errors)
    for fragment in ("DAILY_SUMMARY_TIME", "WEEKLY_SUMMARY_DAY", "TIMEZONE", "MESSAGE_RETENTION_DAYS",
                     "OPENROUTER_API_KEY", "TELEGRAM_GROUP_CHAT_IDS"):
        assert fragment in text


def test_secrets_are_masked_in_logs_and_tracebacks():
    token = "123456789:AAEtesttokenvalue_abcdefghijklmnopqrs"
    key = "sk-or-v1-0123456789abcdef"
    formatter = SecretMaskingFormatter("%(message)s", [token, key])
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{token}/getUpdates failed, key={key}")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "ошибка %s", (token,), exc_info=True)
        import sys
        record.exc_info = sys.exc_info()
    output = formatter.format(record)
    assert token not in output and key not in output
    assert "***" in output
    assert mask_secrets("Authorization: Bearer abcdefghijkl12345") == "Authorization: Bearer ***"


def test_api_key_sent_only_in_header(tmp_path):
    import asyncio

    async def scenario():
        h = build_harness(tmp_path)
        try:
            h.add(CHAT_A, 1, 60)
            h.add(CHAT_A, 2, 30)
            from service import PERIOD_24H, ManualRequest

            await h.service.run_manual(ManualRequest(CHAT_A, 1, PERIOD_24H))
            assert h.llm.headers[0]["authorization"] == f"Bearer {h.settings.openrouter_api_key}"
            assert h.settings.openrouter_api_key not in str(h.llm.requests)
        finally:
            await h.close()

    asyncio.run(scenario())


async def test_updates_are_routed_by_real_ptb_application(tmp_path):
    h = build_harness(tmp_path)
    try:
        application = Application.builder().token("123456789:AAEtesttokenvalue_abcdefghijklmnopqrs").updater(None).build()
        application.bot._bot_user = User(id=123456789, first_name="Bot", is_bot=True, username="summary_test_bot")
        application._initialized = True  # initialize() вызвал бы getMe по сети
        calls = []

        async def fake_summary(update, context):
            calls.append(context.args)

        h.app.cmd_summary = fake_summary
        h.app.register(application)

        def upd(uid, **kw):
            message = tg_message(**kw)
            message.set_bot(application.bot)
            return Update(update_id=uid, message=message)

        await application.process_update(upd(1, chat_id=CHAT_A, message_id=1, text="обычное сообщение"))
        await application.process_update(upd(2, chat_id=CHAT_A, message_id=2, text="/summary 7d"))
        await application.process_update(upd(3, chat_id=CHAT_A, message_id=3, text="/summary@summary_test_bot"))
        await application.process_update(upd(4, chat_id=CHAT_A, message_id=4, text=None, caption="подпись к фото"))

        cmd = tg_message(CHAT_A, 5, text="/summary", edit_date=datetime.fromtimestamp(T0 + 5, timezone.utc))
        cmd.set_bot(application.bot)
        await application.process_update(Update(update_id=5, edited_message=cmd))  # правка команды игнорируется

        stored = [m.text for m in h.db.get_messages(CHAT_A, 0, T0 + 10)]
        assert stored == ["обычное сообщение", "подпись к фото"]  # команды не сохраняются как переписка
        assert calls == [["7d"], []]
    finally:
        await h.close()
