"""Резервное копирование SQLite.

Используется задачей обслуживания (раз в сутки) и вручную:
    docker compose exec bot python backup.py
Копия делается через SQLite Online Backup API — консистентно, без остановки бота.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from database import Database

BACKUP_PREFIX = "bot-"


def create_backup(db: Database, backup_dir: str, keep: int, now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{BACKUP_PREFIX}{stamp}.db"
    db.backup_to(str(path))
    backups = sorted(directory.glob(f"{BACKUP_PREFIX}*.db"))
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink(missing_ok=True)
    return str(path)


def main() -> int:
    db_path = os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parent / "data" / "messages.db"))
    backup_dir = os.environ.get("BACKUP_DIR", str(Path(db_path).parent / "backups"))
    keep = int(os.environ.get("BACKUP_KEEP", "7"))
    if not Path(db_path).exists():
        print(f"База данных не найдена: {db_path}", file=sys.stderr)
        return 1
    db = Database(db_path)
    try:
        path = create_backup(db, backup_dir, max(keep, 1))
    finally:
        db.conn.close()
    print(f"Резервная копия создана: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
