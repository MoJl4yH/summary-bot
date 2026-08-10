"""Конфигурация бота из .env"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# ID групповых чатов через запятую, например: -1001234567890,-1009876543210
TELEGRAM_GROUP_CHAT_IDS = [
    cid.strip() for cid in os.getenv("TELEGRAM_GROUP_CHAT_IDS", "").split(",") if cid.strip()
]

# Время ежедневной отправки саммари в формате HH:MM
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "22:00")
# Таймзона, в которой интерпретируется SUMMARY_TIME и отображается время сообщений
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# LLM (OpenRouter)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-3-flash-preview")

# Хранилище сообщений
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "messages.db"))

PROMPT_PATH = BASE_DIR / "prompts" / "daily_summary.txt"


def validate_config() -> list[str]:
    """Возвращает список ошибок конфигурации (пустой, если всё в порядке)"""
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not TELEGRAM_GROUP_CHAT_IDS:
        errors.append("TELEGRAM_GROUP_CHAT_IDS не задан (ID группы, например -1001234567890)")
    if not OPENROUTER_API_KEY:
        errors.append("OPENROUTER_API_KEY не задан")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", SUMMARY_TIME):
        errors.append(f"SUMMARY_TIME='{SUMMARY_TIME}' — неверный формат, ожидается HH:MM")
    return errors


def parse_summary_time() -> tuple[int, int]:
    """Разбирает SUMMARY_TIME в (час, минута)"""
    hour, minute = SUMMARY_TIME.split(":")
    return int(hour), int(minute)
