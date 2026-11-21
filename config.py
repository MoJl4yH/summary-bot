"""Конфигурация бота из переменных окружения (.env).

Все параметры читаются один раз при старте и валидируются целиком: при ошибках
бот не запускается и выводит полный список проблем.

Совместимость со старой версией проекта:
  SUMMARY_TIME  -> используется как DAILY_SUMMARY_TIME, если последний не задан.
  LLM_MODEL, TELEGRAM_GROUP_CHAT_IDS, DATABASE_PATH, TIMEZONE — без изменений.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_VERSION = "2.0.0"

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"

WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "понедельник": 0, "пн": 0,
    "tuesday": 1, "tue": 1, "вторник": 1, "вт": 1,
    "wednesday": 2, "wed": 2, "среда": 2, "ср": 2,
    "thursday": 3, "thu": 3, "четверг": 3, "чт": 3,
    "friday": 4, "fri": 4, "пятница": 4, "пт": 4,
    "saturday": 5, "sat": 5, "суббота": 5, "сб": 5,
    "sunday": 6, "sun": 6, "воскресенье": 6, "вс": 6,
}
WEEKDAY_NAMES_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

_TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str
    group_chat_ids: frozenset[int]
    allowed_user_ids: frozenset[int]
    ignore_bot_messages: bool
    summary_thread_ids: dict[int, int]  # chat_id -> message_thread_id для плановых сводок (форумы)
    # Расписание
    timezone: str
    daily_enabled: bool
    daily_time: time
    weekly_enabled: bool
    weekly_day: int
    weekly_time: time
    missed_run_max_delay_hours: float
    scheduled_max_attempts: int
    scheduler_tick_seconds: float
    # Сводки
    manual_cooldown_seconds: int
    min_messages: int
    post_empty_auto_summary: bool
    max_message_chars: int
    # Хранение
    message_retention_days: int
    database_path: str
    backup_enabled: bool
    backup_dir: str
    backup_keep: int
    # OpenRouter
    openrouter_api_key: str
    openrouter_base_url: str
    llm_model: str
    llm_temperature: float
    llm_max_output_tokens: int
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_invalid_json_retries: int
    llm_chunk_max_chars: int
    llm_max_input_chars: int
    llm_max_calls_per_summary: int
    llm_daily_budget_usd: float
    # Служебное
    heartbeat_path: str
    log_level: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def summary_thread_id(self, chat_id: int) -> int | None:
        return self.summary_thread_ids.get(chat_id, self.summary_thread_ids.get(0))

    def secrets(self) -> list[str]:
        return [s for s in (self.telegram_bot_token, self.openrouter_api_key) if s]


class _Reader:
    """Читает значения из окружения, накапливая ошибки вместо исключений."""

    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.errors: list[str] = []

    def raw(self, name: str, *fallbacks: str) -> str | None:
        for key in (name, *fallbacks):
            value = self.env.get(key)
            if value is not None and value.strip() != "":
                return value.strip()
        return None

    def str(self, name: str, default: str = "", *fallbacks: str) -> str:
        value = self.raw(name, *fallbacks)
        return default if value is None else value

    def bool(self, name: str, default: bool) -> bool:
        value = self.raw(name)
        if value is None:
            return default
        low = value.lower()
        if low in ("1", "true", "yes", "on", "да"):
            return True
        if low in ("0", "false", "no", "off", "нет"):
            return False
        self.errors.append(f"{name}='{value}' — ожидается true/false")
        return default

    def int(self, name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        value = self.raw(name)
        if value is None:
            return default
        try:
            result = int(value)
        except ValueError:
            self.errors.append(f"{name}='{value}' — ожидается целое число")
            return default
        return self._check_range(name, result, minimum, maximum)

    def float(self, name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
        value = self.raw(name)
        if value is None:
            return default
        try:
            result = float(value)
        except ValueError:
            self.errors.append(f"{name}='{value}' — ожидается число")
            return default
        return self._check_range(name, result, minimum, maximum)

    def _check_range(self, name, value, minimum, maximum):
        if minimum is not None and value < minimum:
            self.errors.append(f"{name}={value} — должно быть не меньше {minimum}")
        if maximum is not None and value > maximum:
            self.errors.append(f"{name}={value} — должно быть не больше {maximum}")
        return value

    def time(self, name: str, default: str, *fallbacks: str) -> time:
        value = self.raw(name, *fallbacks) or default
        match = _TIME_RE.fullmatch(value)
        if not match:
            self.errors.append(f"{name}='{value}' — неверный формат, ожидается HH:MM")
            match = _TIME_RE.fullmatch(default)
        return time(int(match.group(1)), int(match.group(2)))

    def weekday(self, name: str, default: str) -> int:
        value = (self.raw(name) or default).lower()
        if value.isdigit() and 1 <= int(value) <= 7:
            return int(value) - 1  # 1 = понедельник ... 7 = воскресенье
        if value in WEEKDAYS:
            return WEEKDAYS[value]
        self.errors.append(f"{name}='{value}' — ожидается день недели (monday..sunday или 1..7)")
        return WEEKDAYS[default]

    def thread_map(self, name: str) -> dict[int, int]:
        """«123» — тема 123 во всех группах; «-100…:123,-100…:456» — своя тема для каждой группы."""
        value = self.raw(name)
        if value is None:
            return {}
        result: dict[int, int] = {}
        for part in re.split(r"[,\s;]+", value):
            if not part:
                continue
            chat, sep, thread = part.rpartition(":")
            try:
                result[int(chat) if sep else 0] = int(thread)
            except ValueError:
                self.errors.append(f"{name}: '{part}' — ожидается номер темы или chat_id:номер_темы")
        return result

    def int_set(self, name: str, *fallbacks: str) -> frozenset[int]:
        value = self.raw(name, *fallbacks)
        if value is None:
            return frozenset()
        result = set()
        for part in re.split(r"[,\s;]+", value):
            if not part:
                continue
            try:
                result.add(int(part))
            except ValueError:
                self.errors.append(f"{name}: '{part}' — не является числовым идентификатором")
        return frozenset(result)


def load_settings(env: Mapping[str, str] | None = None, *, use_dotenv: bool = True) -> Settings:
    """Загружает и валидирует настройки. Бросает ConfigError со списком всех ошибок."""
    if env is None:
        if use_dotenv:
            from dotenv import load_dotenv

            # Переменные окружения процесса имеют приоритет над .env
            load_dotenv(BASE_DIR / ".env", override=False)
        env = os.environ

    r = _Reader(env)
    default_db = str(BASE_DIR / "data" / "messages.db")

    timezone_name = r.str("TIMEZONE", "Europe/Moscow")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        r.errors.append(f"TIMEZONE='{timezone_name}' — неизвестный часовой пояс (пример: Europe/Moscow)")
        timezone_name = "UTC"

    settings = Settings(
        telegram_bot_token=r.str("TELEGRAM_BOT_TOKEN"),
        group_chat_ids=r.int_set("TELEGRAM_GROUP_CHAT_IDS", "ALLOWED_CHAT_IDS"),
        allowed_user_ids=r.int_set("ALLOWED_USER_IDS"),
        ignore_bot_messages=r.bool("IGNORE_BOT_MESSAGES", True),
        summary_thread_ids=r.thread_map("SUMMARY_THREAD_ID"),
        timezone=timezone_name,
        daily_enabled=r.bool("DAILY_SUMMARY_ENABLED", True),
        daily_time=r.time("DAILY_SUMMARY_TIME", "22:00", "SUMMARY_TIME"),
        weekly_enabled=r.bool("WEEKLY_SUMMARY_ENABLED", True),
        weekly_day=r.weekday("WEEKLY_SUMMARY_DAY", "sunday"),
        weekly_time=r.time("WEEKLY_SUMMARY_TIME", "23:00"),
        missed_run_max_delay_hours=r.float("MISSED_SUMMARY_MAX_DELAY_HOURS", 12.0, 0, 24 * 7),
        scheduled_max_attempts=r.int("SCHEDULED_SUMMARY_MAX_ATTEMPTS", 5, 1, 50),
        scheduler_tick_seconds=r.float("SCHEDULER_TICK_SECONDS", 30.0, 1, 3600),
        manual_cooldown_seconds=r.int("MANUAL_SUMMARY_COOLDOWN_SECONDS", 3600, 0),
        min_messages=r.int("MIN_MESSAGES_FOR_SUMMARY", 5, 1),
        post_empty_auto_summary=r.bool("POST_EMPTY_AUTO_SUMMARY", False),
        max_message_chars=r.int("MAX_MESSAGE_CHARS", 2000, 100, 10000),
        message_retention_days=r.int("MESSAGE_RETENTION_DAYS", 30, 8, 3650),
        database_path=r.str("DATABASE_PATH", default_db),
        backup_enabled=r.bool("BACKUP_ENABLED", True),
        backup_dir=r.str("BACKUP_DIR", str(Path(r.str("DATABASE_PATH", default_db)).parent / "backups")),
        backup_keep=r.int("BACKUP_KEEP", 7, 1, 365),
        openrouter_api_key=r.str("OPENROUTER_API_KEY"),
        openrouter_base_url=r.str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        llm_model=r.str("LLM_MODEL", "google/gemini-3-flash-preview", "OPENROUTER_MODEL"),
        llm_temperature=r.float("LLM_TEMPERATURE", 0.2, 0, 2),
        llm_max_output_tokens=r.int("LLM_MAX_OUTPUT_TOKENS", 4000, 256, 200000),
        llm_timeout_seconds=r.float("LLM_TIMEOUT_SECONDS", 120.0, 5, 1800),
        llm_max_retries=r.int("LLM_MAX_RETRIES", 4, 0, 10),
        llm_invalid_json_retries=r.int("LLM_INVALID_JSON_RETRIES", 1, 0, 5),
        llm_chunk_max_chars=r.int("LLM_CHUNK_MAX_CHARS", 60000, 2000),
        llm_max_input_chars=r.int("LLM_MAX_INPUT_CHARS", 400000, 2000),
        llm_max_calls_per_summary=r.int("LLM_MAX_CALLS_PER_SUMMARY", 12, 1, 200),
        llm_daily_budget_usd=r.float("LLM_DAILY_BUDGET_USD", 0.0, 0),
        heartbeat_path=r.str("HEARTBEAT_PATH", "/tmp/summary-bot.heartbeat"),
        log_level=r.str("LOG_LEVEL", "INFO").upper(),
    )

    errors = r.errors
    if not settings.telegram_bot_token:
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not settings.group_chat_ids:
        errors.append("TELEGRAM_GROUP_CHAT_IDS не задан (ID группы, например -1001234567890)")
    if not settings.openrouter_api_key:
        errors.append("OPENROUTER_API_KEY не задан")
    if not settings.openrouter_base_url.startswith("https://"):
        errors.append("OPENROUTER_BASE_URL должен начинаться с https://")
    if settings.llm_max_input_chars < settings.llm_chunk_max_chars:
        errors.append("LLM_MAX_INPUT_CHARS должен быть не меньше LLM_CHUNK_MAX_CHARS")
    if settings.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append(f"LOG_LEVEL='{settings.log_level}' — ожидается DEBUG/INFO/WARNING/ERROR")
    if errors:
        raise ConfigError(errors)
    return settings
