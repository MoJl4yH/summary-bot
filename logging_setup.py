"""Настройка журналирования с маскированием секретов.

Маскирование выполняется над итоговой строкой (включая traceback), поэтому
токен не попадёт в журнал даже из текста исключения или URL запроса.
"""
from __future__ import annotations

import logging
import re
import sys

# Токен Telegram-бота: <bot_id>:<35 символов>; ключи OpenRouter: sk-or-...
_TOKEN_PATTERNS = [
    re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bsk-or-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{10,}"),
]


def mask_secrets(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class SecretMaskingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self._secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record), self._secrets)


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SecretMaskingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets or [])
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx на уровне INFO пишет URL запросов, а URL Bot API содержит токен
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "telegram._bot"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
