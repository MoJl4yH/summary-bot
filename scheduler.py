"""Планировщик ежедневных/еженедельных сводок и задача обслуживания.

Модель расписания:
  * «Слот» — момент запуска (например, 22:00 по TIMEZONE). Окно сводки —
    полуинтервал [слот − 24 ч, слот) или [слот − 7 сут, слот) в абсолютном времени.
  * Каждую итерацию рассматривается только последний наступивший слот каждого
    типа. Если он не старше MISSED_SUMMARY_MAX_DELAY_HOURS и не раньше первой
    установки бота, задача атомарно захватывается в БД по ключу
    (chat_id, kind, period_key) — поэтому после перезапуска пропущенная сводка
    будет выполнена, а опубликованная не продублируется.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from datetime import timezone
from zoneinfo import ZoneInfo

from backup import create_backup
from config import Settings
from database import Database
from service import DAY, WEEK, SummaryService

logger = logging.getLogger(__name__)

RUN_LEASE_SECONDS = 3600
MAINTENANCE_INTERVAL = DAY
SERVICE_HISTORY_DAYS = 365


def _at(d: date, at: dtime, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, at.hour, at.minute, tzinfo=tz)


def latest_daily_slot(now: datetime, tz: ZoneInfo, at: dtime) -> datetime:
    local = now.astimezone(tz)
    slot = _at(local.date(), at, tz)
    if slot > local:
        slot = _at(local.date() - timedelta(days=1), at, tz)
    return slot


def next_daily_slot(now: datetime, tz: ZoneInfo, at: dtime) -> datetime:
    return _at(latest_daily_slot(now, tz, at).date() + timedelta(days=1), at, tz)


def latest_weekly_slot(now: datetime, tz: ZoneInfo, weekday: int, at: dtime) -> datetime:
    local = now.astimezone(tz)
    day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    slot = _at(day, at, tz)
    if slot > local:
        slot = _at(day - timedelta(days=7), at, tz)
    return slot


def next_weekly_slot(now: datetime, tz: ZoneInfo, weekday: int, at: dtime) -> datetime:
    return _at(latest_weekly_slot(now, tz, weekday, at).date() + timedelta(days=7), at, tz)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        service: SummaryService,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.db = db
        self.service = service
        self.clock = clock
        self.last_tick_at: float | None = None

    def due_slots(self, now_ts: int) -> list[tuple[str, datetime, int]]:
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        tz = self.settings.tz
        slots = []
        if self.settings.daily_enabled:
            slots.append(("daily", latest_daily_slot(now, tz, self.settings.daily_time), DAY))
        if self.settings.weekly_enabled:
            slots.append(
                ("weekly", latest_weekly_slot(now, tz, self.settings.weekly_day, self.settings.weekly_time), WEEK)
            )
        return slots

    async def tick(self) -> int:
        """Одна итерация: выполняет наступившие и ещё не выполненные задачи. Возвращает их число."""
        now_ts = int(self.clock())
        self.last_tick_at = now_ts
        installed_at = int(self.db.get_meta("installed_at") or 0)
        max_delay = int(self.settings.missed_run_max_delay_hours * 3600)
        executed = 0
        for kind, slot, length in self.due_slots(now_ts):
            slot_ts = int(slot.timestamp())
            if slot_ts < installed_at or now_ts - slot_ts > max_delay:
                continue
            period_key = slot.isoformat()
            for chat_id in sorted(self.settings.group_chat_ids):
                run = self.db.claim_scheduled_run(
                    chat_id, kind, period_key, slot_ts - length, slot_ts, now_ts,
                    RUN_LEASE_SECONDS, self.settings.scheduled_max_attempts,
                )
                if run is None:
                    continue
                logger.info(
                    "Запуск плановой сводки %s для чата %s за период до %s (попытка %s)",
                    kind, chat_id, period_key, run.attempts,
                )
                await self.service.run_scheduled(run)
                executed += 1
        return executed

    async def run_forever(self, stop: asyncio.Event) -> None:
        tz = self.settings.tz
        now = datetime.now(timezone.utc)
        if self.settings.daily_enabled:
            logger.info("Ежедневная сводка: следующая %s", next_daily_slot(now, tz, self.settings.daily_time))
        if self.settings.weekly_enabled:
            logger.info(
                "Еженедельная сводка: следующая %s",
                next_weekly_slot(now, tz, self.settings.weekly_day, self.settings.weekly_time),
            )
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Ошибка итерации планировщика")
            await _sleep_or_stop(stop, self.settings.scheduler_tick_seconds)


class Maintenance:
    """Удаление устаревших сообщений, очистка служебной истории, резервная копия БД."""

    def __init__(self, settings: Settings, db: Database, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.db = db
        self.clock = clock

    def run_once(self) -> dict:
        now = int(self.clock())
        deleted = self.db.delete_messages_before(now - self.settings.message_retention_days * DAY)
        pruned = self.db.prune_history(now - SERVICE_HISTORY_DAYS * DAY)
        backup_path = None
        if self.settings.backup_enabled:
            backup_path = create_backup(self.db, self.settings.backup_dir, self.settings.backup_keep, now)
        self.db.set_meta("last_maintenance_at", str(now))
        logger.info(
            "Обслуживание: удалено сообщений старше %s дн.: %s, служебных записей: %s, резервная копия: %s",
            self.settings.message_retention_days, deleted, pruned, backup_path or "отключена",
        )
        return {"deleted_messages": deleted, "pruned": pruned, "backup": backup_path}

    def is_due(self) -> bool:
        last = int(self.db.get_meta("last_maintenance_at") or 0)
        return int(self.clock()) - last >= MAINTENANCE_INTERVAL

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if self.is_due():
                    self.run_once()
            except Exception:
                logger.exception("Ошибка задачи обслуживания")
            await _sleep_or_stop(stop, 3600)
