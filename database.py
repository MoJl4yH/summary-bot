"""Хранилище SQLite: сообщения, состояние расписания, ограничения ручных запросов,
метаданные сводок и учёт расходов LLM.

Все времена хранятся как целые UNIX-секунды (UTC). Соединение одно на процесс
(бот однопоточный, asyncio); изменения состояния выполняются в транзакциях
BEGIN IMMEDIATE, что исключает гонки при атомарном «захвате» задач.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id             INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    user_id             INTEGER,
    username            TEXT,
    display_name        TEXT    NOT NULL,
    date                INTEGER NOT NULL,
    text                TEXT    NOT NULL,
    reply_to_message_id INTEGER,
    message_thread_id   INTEGER,
    edit_date           INTEGER,
    received_at         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_state (
    chat_id           INTEGER PRIMARY KEY,
    in_progress_until INTEGER,
    in_progress_by    INTEGER,
    last_success_at   INTEGER,
    last_request_at   INTEGER
);

CREATE TABLE IF NOT EXISTS summaries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    kind              TEXT    NOT NULL CHECK (kind IN ('daily', 'weekly', 'manual')),
    period_key        TEXT,
    window_start      INTEGER NOT NULL,
    window_end        INTEGER NOT NULL,
    requested_by      INTEGER,
    status            TEXT    NOT NULL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    llm_calls         INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL,
    model             TEXT,
    parts_json        TEXT,
    parts_sent        INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    sent_at           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_summaries_chat_created ON summaries(chat_id, created_at);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    chat_id         INTEGER NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('daily', 'weekly')),
    period_key      TEXT    NOT NULL,
    window_start    INTEGER NOT NULL,
    window_end      INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    lease_until     INTEGER,
    next_attempt_at INTEGER,
    summary_id      INTEGER REFERENCES summaries(id),
    error           TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, kind, period_key)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        INTEGER NOT NULL,
    chat_id           INTEGER,
    summary_id        INTEGER,
    model             TEXT,
    status            TEXT    NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          REAL,
    latency_ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
"""


@dataclass(frozen=True)
class MessageRecord:
    chat_id: int
    message_id: int
    user_id: int | None
    username: str | None
    display_name: str
    date: int
    text: str
    reply_to_message_id: int | None = None
    message_thread_id: int | None = None
    edit_date: int | None = None


@dataclass(frozen=True)
class ManualClaim:
    status: str  # "ok" | "busy" | "cooldown"
    retry_at: int | None = None


@dataclass(frozen=True)
class ScheduledRun:
    chat_id: int
    kind: str
    period_key: str
    window_start: int
    window_end: int
    status: str
    attempts: int
    summary_id: int | None


@dataclass(frozen=True)
class SummaryRow:
    id: int
    chat_id: int
    kind: str
    period_key: str | None
    window_start: int
    window_end: int
    status: str
    message_count: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    model: str | None
    parts: list[str]
    parts_sent: int
    error: str | None
    created_at: int
    updated_at: int
    sent_at: int | None


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")

    # ------------------------------------------------------------------ общее

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def initialize(self, now: int) -> None:
        """Применяет миграции и приводит состояние к консистентному после перезапуска."""
        self._migrate()
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('installed_at', ?)", (str(now),))
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('started_at', ?)", (str(now),))
            # Процесс единственный: всё, что было «в работе» до перезапуска, прервано.
            c.execute(
                "UPDATE manual_state SET in_progress_until = NULL, in_progress_by = NULL "
                "WHERE in_progress_until IS NOT NULL"
            )
            c.execute(
                "UPDATE scheduled_runs SET lease_until = ?, updated_at = ? WHERE status = 'running'",
                (now, now),
            )
            c.execute(
                "UPDATE summaries SET status = 'failed', error = 'прервано перезапуском', updated_at = ? "
                "WHERE status = 'generating'",
                (now,),
            )
        logger.info("База данных готова: %s (схема v%s)", self.path, SCHEMA_VERSION)

    def ensure_schema(self) -> None:
        """Только миграция схемы — без сброса состояния (для внешних утилит, напр. импорта)."""
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Схема БД v{version} новее, чем поддерживает приложение (v{SCHEMA_VERSION})"
            )
        if version < 1:
            with self.transaction() as c:
                for statement in _SCHEMA_V1.split(";"):
                    if statement.strip():
                        c.execute(statement)
                legacy = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'daily_logs'"
                ).fetchone()
                if legacy:
                    moved = c.execute(
                        """
                        INSERT OR IGNORE INTO messages
                            (chat_id, message_id, user_id, username, display_name, date, text,
                             reply_to_message_id, message_thread_id, edit_date, received_at)
                        SELECT CAST(chat_id AS INTEGER), CAST(message_id AS INTEGER),
                               CAST(user_id AS INTEGER), NULL, user_name,
                               CAST(strftime('%s', created_at) AS INTEGER), content,
                               CAST(reply_to_message_id AS INTEGER), NULL, NULL,
                               CAST(strftime('%s', created_at) AS INTEGER)
                        FROM daily_logs
                        """
                    ).rowcount
                    c.execute("DROP TABLE daily_logs")
                    logger.info("Миграция: перенесено %s сообщений из устаревшей таблицы daily_logs", moved)
                c.execute("PRAGMA user_version = 1")

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -------------------------------------------------------------- сообщения

    def upsert_message(self, rec: MessageRecord, now: int, *, is_edit: bool = False) -> bool:
        """Сохраняет сообщение. Повторная доставка того же сообщения игнорируется,
        правка (edited_message) обновляет текст. Возвращает True, если строка изменена."""
        params = (
            rec.chat_id, rec.message_id, rec.user_id, rec.username, rec.display_name, rec.date,
            rec.text, rec.reply_to_message_id, rec.message_thread_id, rec.edit_date, now,
        )
        insert = (
            "INSERT INTO messages (chat_id, message_id, user_id, username, display_name, date, text, "
            "reply_to_message_id, message_thread_id, edit_date, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        )
        if is_edit:
            sql = insert + (
                "ON CONFLICT(chat_id, message_id) DO UPDATE SET text = excluded.text, "
                "edit_date = excluded.edit_date, display_name = excluded.display_name, "
                "username = excluded.username "
                "WHERE COALESCE(messages.edit_date, 0) <= COALESCE(excluded.edit_date, 0)"
            )
        else:
            sql = insert + "ON CONFLICT(chat_id, message_id) DO NOTHING"
        return self.conn.execute(sql, params).rowcount > 0

    def get_messages(self, chat_id: int, start_ts: int, end_ts: int) -> list[MessageRecord]:
        """Сообщения чата в полуинтервале [start_ts, end_ts) по возрастанию времени."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND date >= ? AND date < ? "
            "ORDER BY date ASC, message_id ASC",
            (chat_id, start_ts, end_ts),
        ).fetchall()
        return [
            MessageRecord(
                chat_id=row["chat_id"], message_id=row["message_id"], user_id=row["user_id"],
                username=row["username"], display_name=row["display_name"], date=row["date"],
                text=row["text"], reply_to_message_id=row["reply_to_message_id"],
                message_thread_id=row["message_thread_id"], edit_date=row["edit_date"],
            )
            for row in rows
        ]

    def count_messages(self, chat_id: int, since_ts: int | None = None) -> int:
        if since_ts is None:
            row = self.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND date >= ?", (chat_id, since_ts)
            ).fetchone()
        return row[0]

    def delete_messages_before(self, before_ts: int) -> int:
        """Задача обслуживания: удаление сообщений старше срока хранения."""
        return self.conn.execute("DELETE FROM messages WHERE date < ?", (before_ts,)).rowcount

    def prune_history(self, before_ts: int) -> int:
        """Удаляет старые служебные записи (сводки, учёт LLM, выполненные задачи)."""
        with self.transaction() as c:
            n = c.execute("DELETE FROM llm_usage WHERE created_at < ?", (before_ts,)).rowcount
            n += c.execute(
                "DELETE FROM scheduled_runs WHERE updated_at < ? AND status != 'running'", (before_ts,)
            ).rowcount
            n += c.execute(
                "DELETE FROM summaries WHERE created_at < ? AND id NOT IN "
                "(SELECT summary_id FROM scheduled_runs WHERE summary_id IS NOT NULL)",
                (before_ts,),
            ).rowcount
        return n

    # ---------------------------------------------------- ручные запросы/лимит

    def try_begin_manual(
        self, chat_id: int, user_id: int | None, now: int, cooldown: int, lease: int
    ) -> ManualClaim:
        """Атомарно фиксирует начало ручной генерации для группы.

        ok       — генерация разрешена и помечена как выполняемая;
        busy     — в группе уже идёт генерация;
        cooldown — лимит исчерпан, retry_at — когда можно снова.
        """
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO manual_state(chat_id) VALUES (?)", (chat_id,))
            row = c.execute("SELECT * FROM manual_state WHERE chat_id = ?", (chat_id,)).fetchone()
            c.execute("UPDATE manual_state SET last_request_at = ? WHERE chat_id = ?", (now, chat_id))
            if row["in_progress_until"] is not None and row["in_progress_until"] > now:
                return ManualClaim("busy")
            last = row["last_success_at"]
            if last is not None and cooldown > 0 and now < last + cooldown:
                return ManualClaim("cooldown", last + cooldown)
            c.execute(
                "UPDATE manual_state SET in_progress_until = ?, in_progress_by = ? WHERE chat_id = ?",
                (now + lease, user_id, chat_id),
            )
            return ManualClaim("ok")

    def finish_manual(self, chat_id: int, *, success: bool, now: int) -> None:
        """Снимает блокировку. Лимит расходуется только при успешной генерации и отправке."""
        with self.transaction() as c:
            if success:
                c.execute(
                    "UPDATE manual_state SET in_progress_until = NULL, in_progress_by = NULL, "
                    "last_success_at = ? WHERE chat_id = ?",
                    (now, chat_id),
                )
            else:
                c.execute(
                    "UPDATE manual_state SET in_progress_until = NULL, in_progress_by = NULL WHERE chat_id = ?",
                    (chat_id,),
                )

    def get_manual_state(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM manual_state WHERE chat_id = ?", (chat_id,)).fetchone()

    # ------------------------------------------------------------- расписание

    def claim_scheduled_run(
        self,
        chat_id: int,
        kind: str,
        period_key: str,
        window_start: int,
        window_end: int,
        now: int,
        lease: int,
        max_attempts: int,
    ) -> ScheduledRun | None:
        """Атомарно захватывает задачу расписания за период. Возвращает None, если
        задача уже выполнена, выполняется или исчерпала попытки."""
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM scheduled_runs WHERE chat_id = ? AND kind = ? AND period_key = ?",
                (chat_id, kind, period_key),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO scheduled_runs (chat_id, kind, period_key, window_start, window_end, status, "
                    "attempts, lease_until, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'running', 1, ?, ?, ?)",
                    (chat_id, kind, period_key, window_start, window_end, now + lease, now, now),
                )
                return ScheduledRun(chat_id, kind, period_key, window_start, window_end, "running", 1, None)

            status = row["status"]
            if status in ("done", "skipped", "gave_up"):
                return None
            if status == "running" and row["lease_until"] is not None and row["lease_until"] > now:
                return None
            if status == "failed" and row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
                return None
            if row["attempts"] >= max_attempts:
                c.execute(
                    "UPDATE scheduled_runs SET status = 'gave_up', lease_until = NULL, updated_at = ? "
                    "WHERE chat_id = ? AND kind = ? AND period_key = ?",
                    (now, chat_id, kind, period_key),
                )
                logger.error("Задача %s/%s/%s: исчерпаны попытки (%s)", chat_id, kind, period_key, row["attempts"])
                return None
            attempts = row["attempts"] + 1
            c.execute(
                "UPDATE scheduled_runs SET status = 'running', attempts = ?, lease_until = ?, updated_at = ? "
                "WHERE chat_id = ? AND kind = ? AND period_key = ?",
                (attempts, now + lease, now, chat_id, kind, period_key),
            )
            return ScheduledRun(
                chat_id, kind, period_key, row["window_start"], row["window_end"], "running",
                attempts, row["summary_id"],
            )

    def set_run_summary(self, chat_id: int, kind: str, period_key: str, summary_id: int, now: int) -> None:
        self.conn.execute(
            "UPDATE scheduled_runs SET summary_id = ?, updated_at = ? WHERE chat_id = ? AND kind = ? AND period_key = ?",
            (summary_id, now, chat_id, kind, period_key),
        )

    def finish_scheduled_run(
        self,
        chat_id: int,
        kind: str,
        period_key: str,
        *,
        status: str,
        now: int,
        error: str | None = None,
        next_attempt_at: int | None = None,
    ) -> None:
        assert status in ("done", "skipped", "failed", "gave_up")
        self.conn.execute(
            "UPDATE scheduled_runs SET status = ?, error = ?, next_attempt_at = ?, lease_until = NULL, "
            "updated_at = ? WHERE chat_id = ? AND kind = ? AND period_key = ?",
            (status, error, next_attempt_at, now, chat_id, kind, period_key),
        )

    def get_scheduled_run(self, chat_id: int, kind: str, period_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scheduled_runs WHERE chat_id = ? AND kind = ? AND period_key = ?",
            (chat_id, kind, period_key),
        ).fetchone()

    # ----------------------------------------------------------------- сводки

    def create_summary(
        self,
        chat_id: int,
        kind: str,
        window_start: int,
        window_end: int,
        now: int,
        *,
        period_key: str | None = None,
        requested_by: int | None = None,
        model: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO summaries (chat_id, kind, period_key, window_start, window_end, requested_by, status, "
            "model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'generating', ?, ?, ?)",
            (chat_id, kind, period_key, window_start, window_end, requested_by, model, now, now),
        )
        return cur.lastrowid

    _SUMMARY_FIELDS = {
        "status", "message_count", "llm_calls", "prompt_tokens", "completion_tokens", "cost_usd",
        "model", "parts_sent", "error", "sent_at",
    }

    def update_summary(self, summary_id: int, now: int, *, parts: list[str] | None = None, **fields) -> None:
        unknown = set(fields) - self._SUMMARY_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля: {unknown}")
        if parts is not None:
            fields["parts_json"] = json.dumps(parts, ensure_ascii=False)
        fields["updated_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(
            f"UPDATE summaries SET {assignments} WHERE id = ?", (*fields.values(), summary_id)
        )

    def get_summary(self, summary_id: int) -> SummaryRow | None:
        row = self.conn.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
        return self._summary_row(row) if row else None

    def last_summary(self, chat_id: int) -> SummaryRow | None:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
        return self._summary_row(row) if row else None

    @staticmethod
    def _summary_row(row: sqlite3.Row) -> SummaryRow:
        return SummaryRow(
            id=row["id"], chat_id=row["chat_id"], kind=row["kind"], period_key=row["period_key"],
            window_start=row["window_start"], window_end=row["window_end"], status=row["status"],
            message_count=row["message_count"], llm_calls=row["llm_calls"],
            prompt_tokens=row["prompt_tokens"], completion_tokens=row["completion_tokens"],
            cost_usd=row["cost_usd"], model=row["model"],
            parts=json.loads(row["parts_json"]) if row["parts_json"] else [],
            parts_sent=row["parts_sent"], error=row["error"], created_at=row["created_at"],
            updated_at=row["updated_at"], sent_at=row["sent_at"],
        )

    # ----------------------------------------------------------- расходы LLM

    def record_llm_usage(
        self,
        now: int,
        *,
        chat_id: int | None,
        summary_id: int | None,
        model: str | None,
        status: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage (created_at, chat_id, summary_id, model, status, prompt_tokens, "
            "completion_tokens, cost_usd, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, chat_id, summary_id, model, status, prompt_tokens, completion_tokens, cost_usd, latency_ms),
        )

    def llm_cost_since(self, since_ts: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE created_at >= ?", (since_ts,)
        ).fetchone()
        return float(row[0])

    def llm_calls_since(self, since_ts: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE created_at >= ?", (since_ts,)
        ).fetchone()[0]

    # ---------------------------------------------------------- резервные копии

    def backup_to(self, dest_path: str) -> None:
        """Консистентная копия работающей БД (SQLite Online Backup API)."""
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path + ".tmp"
        dest = sqlite3.connect(tmp_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dest_path)
