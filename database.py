"""Хранилище сообщений для дневного саммари (SQLite)"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import DATABASE_PATH


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo (в таком виде хранится created_at)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL,
    message_id TEXT NOT NULL,
    content TEXT NOT NULL,
    reply_to_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_logs_chat_created ON daily_logs(chat_id, created_at);
"""


@dataclass
class DailyLog:
    id: int
    chat_id: str
    user_id: str
    user_name: str
    message_id: str
    content: str
    reply_to_message_id: str | None
    created_at: datetime  # naive UTC


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
    logger.info(f"База данных инициализирована: {DATABASE_PATH}")


def save_message(
    chat_id: str,
    user_id: str,
    user_name: str,
    message_id: str,
    content: str,
    reply_to_message_id: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO daily_logs (chat_id, user_id, user_name, message_id, content, reply_to_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                user_id,
                user_name,
                message_id,
                content,
                reply_to_message_id,
                utcnow().isoformat(sep=" ", timespec="seconds"),
            ),
        )


def get_messages_since(chat_id: str, since_utc: datetime) -> list[DailyLog]:
    """Сообщения чата начиная с since_utc, по возрастанию времени"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_logs WHERE chat_id = ? AND created_at >= ? ORDER BY created_at ASC, id ASC",
            (chat_id, since_utc.isoformat(sep=" ", timespec="seconds")),
        ).fetchall()
    return [
        DailyLog(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            message_id=row["message_id"],
            content=row["content"],
            reply_to_message_id=row["reply_to_message_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


def delete_messages(log_ids: list[int]) -> int:
    """Удаляет обработанные сообщения по их id"""
    if not log_ids:
        return 0
    placeholders = ",".join("?" * len(log_ids))
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM daily_logs WHERE id IN ({placeholders})", log_ids)
    return cur.rowcount


def cleanup_old_logs(before_utc: datetime) -> int:
    """Удаляет записи старше before_utc (страховка, если плановая генерация не сработала)"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM daily_logs WHERE created_at < ?",
            (before_utc.isoformat(sep=" ", timespec="seconds"),),
        )
    return cur.rowcount
