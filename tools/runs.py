"""Просмотр и ручной повтор плановых сводок.

    docker compose exec bot python tools/runs.py list
    docker compose exec bot python tools/runs.py retry daily      # последняя неудавшаяся ежедневная
    docker compose exec bot python tools/runs.py retry weekly --period 2026-11-22T23:00:00+03:00

Повтор подхватывается работающим ботом в течение SCHEDULER_TICK_SECONDS (30 с). Если текст
сводки уже был сгенерирован, он отправляется из базы без повторного обращения к LLM.
Повтор возможен, пока с запланированного момента прошло не больше MISSED_SUMMARY_MAX_DELAY_HOURS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "data/messages.db"), help="путь к SQLite")
    sub = parser.add_subparsers(dest="cmd", required=True)
    list_cmd = sub.add_parser("list", help="последние плановые задачи")
    list_cmd.add_argument("--limit", type=int, default=10)
    retry = sub.add_parser("retry", help="повторить неудавшуюся плановую сводку")
    retry.add_argument("kind", choices=["daily", "weekly"])
    retry.add_argument("--period", help="period_key из списка (по умолчанию — последний неудавшийся)")
    retry.add_argument("--chat", type=int, help="chat_id (по умолчанию — все группы)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"База не найдена: {args.db}", file=sys.stderr)
        return 1
    db = Database(args.db)
    try:
        rows = db.recent_scheduled_runs(200)
        if args.cmd == "list":
            for r in rows[: args.limit]:
                parts = len(json.loads(r["parts_json"])) if r["parts_json"] else 0
                sent = f", отправлено частей {r['parts_sent']}/{parts}" if parts else ""
                print(f"{r['chat_id']}  {r['kind']:6}  {r['period_key']}  {r['status']:8}  "
                      f"попыток {r['attempts']}{sent}" + (f"\n    ошибка: {r['error']}" if r["error"] else ""))
            if not rows:
                print("Плановых задач ещё не было")
            return 0

        candidates = [
            r for r in rows
            if r["kind"] == args.kind and r["status"] in ("failed", "gave_up")
            and (args.chat is None or r["chat_id"] == args.chat)
            and (args.period is None or r["period_key"] == args.period)
        ]
        if args.period is None:  # по каждой группе — только самая свежая
            latest: dict[int, object] = {}
            for r in candidates:
                latest.setdefault(r["chat_id"], r)
            candidates = list(latest.values())
        if not candidates:
            print("Нет неудавшихся задач для повтора (см. `list`)")
            return 1
        for r in candidates:
            if db.reset_scheduled_run(r["chat_id"], r["kind"], r["period_key"], int(time.time())):
                ready = r["summary_status"] == "ready"
                print(f"Поставлено в повтор: {r['chat_id']} {r['kind']} {r['period_key']}"
                      + (" — текст готов, LLM не понадобится" if ready else " — сводка будет сгенерирована заново"))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
