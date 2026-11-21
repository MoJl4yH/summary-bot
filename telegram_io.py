"""Отправка сообщений в Telegram с повторами при временных ошибках."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta

# Явно включаем будущее поведение PTB: временные интервалы как datetime.timedelta
os.environ.setdefault("PTB_TIMEDELTA", "1")

from telegram import LinkPreviewOptions, ReplyParameters  # noqa: E402
from telegram.constants import ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError

from formatting import strip_html

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 300


class PublishError(Exception):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class TelegramPublisher:
    def __init__(
        self,
        bot,
        *,
        max_attempts: int = 5,
        base_delay: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.bot = bot
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._sleep = sleep

    async def send(
        self, chat_id: int, html_text: str, *, reply_to: int | None = None, thread_id: int | None = None
    ) -> int:
        """Отправляет одно HTML-сообщение. Возвращает message_id."""
        plain = False
        attempt = 0
        while True:
            kwargs: dict = {
                "chat_id": chat_id,
                "text": strip_html(html_text) if plain else html_text,
                "parse_mode": None if plain else ParseMode.HTML,
                "link_preview_options": LinkPreviewOptions(is_disabled=True),
            }
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            if reply_to:
                kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            try:
                message = await self.bot.send_message(**kwargs)
                return message.message_id
            except RetryAfter as exc:
                delay = _seconds(exc.retry_after) + 1
                if delay > MAX_RETRY_AFTER_SECONDS:
                    raise PublishError(f"Telegram ограничил отправку на {delay:.0f} с", retryable=True) from exc
                error_text = f"ограничение частоты, ожидание {delay:.0f} с"
            except BadRequest as exc:
                text = str(exc).lower()
                if not plain and ("parse entities" in text or "unsupported start tag" in text):
                    logger.warning("Telegram не принял HTML-разметку, отправка простым текстом")
                    plain = True
                    continue
                if thread_id and "thread not found" in text:
                    thread_id = None
                    continue
                hint = ""
                if "topic_closed" in text:
                    hint = (" — тема закрыта для участников; укажите открытую тему в SUMMARY_THREAD_ID "
                            "(номер темы покажет /chatid, отправленная внутри неё)")
                raise PublishError(f"Telegram отклонил сообщение: {exc}{hint}", retryable=False) from exc
            except Forbidden as exc:
                raise PublishError(f"нет доступа к чату: {exc}", retryable=False) from exc
            except ChatMigrated as exc:
                raise PublishError(
                    f"группа преобразована в супергруппу, новый ID {exc.new_chat_id}: обновите TELEGRAM_GROUP_CHAT_IDS",
                    retryable=False,
                ) from exc
            except (NetworkError, TelegramError) as exc:  # TimedOut — подкласс NetworkError
                delay = min(60.0, self.base_delay * (2 ** attempt)) * random.uniform(0.5, 1.0)
                error_text = f"{type(exc).__name__}: {exc}"

            attempt += 1
            if attempt >= self.max_attempts:
                raise PublishError(f"не удалось отправить сообщение: {error_text}", retryable=True)
            logger.warning(
                "Telegram: временная ошибка отправки в чат %s (попытка %s/%s): %s; повтор через %.1f с",
                chat_id, attempt, self.max_attempts, error_text, delay,
            )
            await self._sleep(delay)

    async def send_parts(
        self,
        chat_id: int,
        parts: list[str],
        *,
        start: int = 0,
        reply_to: int | None = None,
        thread_id: int | None = None,
        on_sent: Callable[[int], None] | None = None,
    ) -> None:
        """Отправляет части по порядку; on_sent(n) вызывается после каждой (n — сколько отправлено)."""
        for index in range(start, len(parts)):
            await self.send(chat_id, parts[index], reply_to=reply_to if index == 0 else None, thread_id=thread_id)
            if on_sent:
                on_sent(index + 1)
