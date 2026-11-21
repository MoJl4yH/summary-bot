"""Сводка по экспорту чата из Telegram Desktop (JSON) — для проверки на реальных данных.

Bot API не даёт доступа к истории, поэтому для пробы используется экспорт:
Telegram Desktop → группа → ⋮ → «Экспорт истории чата» (JSON или HTML).
В HTML-экспорте нет ID чата: для ссылок на сообщения и импорта укажите --chat-id.

Примеры:
    # оценка объёма без обращения к LLM
    python tools/summarize_export.py ChatExport/result.json --dry-run
    # сводка за последние 24 часа экспорта (относительно последнего сообщения)
    python tools/summarize_export.py ChatExport/result.json --end last
    # за 7 дней, до текущего момента
    python tools/summarize_export.py ChatExport/result.json --period 7d
    # загрузить историю в базу бота (без генерации)
    python tools/summarize_export.py ChatExport/result.json --import-db data/messages.db --no-summary

Сводка НЕ публикуется в Telegram: выводится в консоль и сохраняется в HTML-превью.
Для генерации нужен OPENROUTER_API_KEY (из окружения или .env).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BASE_DIR, ConfigError, load_settings  # noqa: E402
from database import Database, MessageRecord  # noqa: E402
from formatting import format_period, render_summary, split_blocks, strip_html  # noqa: E402
from service import PERIOD_7D, PERIOD_24H  # noqa: E402
from telegram_html_export import parse_html_export  # noqa: E402


# ------------------------------------------------------------------ разбор экспорта

def _text(value) -> str:
    """Поле text экспорта — строка или список строк и объектов-сущностей."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in value if p is not None)
    return ""


def _timestamp(message: dict, key: str, tz: ZoneInfo) -> int | None:
    unix = message.get(f"{key}_unixtime")
    if unix:
        return int(unix)
    raw = message.get(key)
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:  # старые экспорты пишут локальное время компьютера, где делали экспорт
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def chat_id_from_export(chat: dict) -> int:
    """ID в экспорте хранится без префикса -100; восстанавливаем формат Bot API."""
    raw = int(chat["id"])
    kind = str(chat.get("type", ""))
    if raw < 0:
        return raw
    if "supergroup" in kind or "channel" in kind:
        return int(f"-100{raw}")
    if "group" in kind:
        return -raw
    return raw


def select_chat(data: dict, query: str | None) -> dict:
    if "messages" in data:
        return data
    chats = (data.get("chats") or {}).get("list") or []
    if not chats:
        raise SystemExit("В файле нет сообщений: ожидается result.json экспорта чата Telegram Desktop")
    if query:
        matches = [c for c in chats if str(c.get("id")) == query.lstrip("-").removeprefix("100")
                   or query.lower() in str(c.get("name", "")).lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SystemExit(f"Чат «{query}» не найден в экспорте")
        chats = matches
    listing = "\n".join(f"  {c.get('id')}\t{c.get('type')}\t{c.get('name')}" for c in chats)
    raise SystemExit(f"В экспорте несколько чатов, укажите --chat <id или часть названия>:\n{listing}")


def parse_export(chat: dict, chat_id: int, tz: ZoneInfo) -> list[MessageRecord]:
    records = []
    for message in chat.get("messages", []):
        if message.get("type") != "message":
            continue  # служебные: вход/выход участников, закрепления и т. п.
        text = _text(message.get("text"))
        if not text.strip():
            continue  # медиа без подписи
        date = _timestamp(message, "date", tz)
        if date is None:
            continue
        from_id = str(message.get("from_id") or "")
        user_id = int(from_id[4:]) if from_id.startswith("user") and from_id[4:].isdigit() else None
        reply_to = message.get("reply_to_message_id")
        records.append(MessageRecord(
            chat_id=chat_id,
            message_id=int(message["id"]),
            user_id=user_id,
            username=None,
            display_name=str(message.get("from") or message.get("actor") or "Без имени")[:128],
            date=date,
            text=text,
            reply_to_message_id=int(reply_to) if reply_to else None,
            message_thread_id=None,
            edit_date=_timestamp(message, "edited", tz),
        ))
    records.sort(key=lambda r: (r.date, r.message_id))
    return records


def window_end(arg: str, records: list[MessageRecord], tz: ZoneInfo) -> int:
    if arg == "now":
        return int(time.time())
    if arg == "last":
        if not records:
            raise SystemExit("В экспорте нет текстовых сообщений")
        return records[-1].date + 1
    dt = datetime.fromisoformat(arg)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


# ------------------------------------------------------------------ настройки и вывод

def build_settings(chat_id: int, *, need_key: bool):
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
    env = dict(os.environ)
    # Telegram этой утилите не нужен; пустые значения из .env заменяем заглушками
    defaults = {"TELEGRAM_BOT_TOKEN": "not-used-by-this-tool", "TELEGRAM_GROUP_CHAT_IDS": str(chat_id)}
    if not need_key:
        defaults["OPENROUTER_API_KEY"] = "dry-run"
    for key, value in defaults.items():
        if not env.get(key, "").strip():
            env[key] = value
    try:
        return load_settings(env)
    except ConfigError as exc:
        raise SystemExit("Ошибка конфигурации:\n  " + "\n  ".join(exc.errors)) from None


PREVIEW_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Превью сводки</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,sans-serif;max-width:680px;margin:24px auto;padding:0 16px;background:#e7ebf0;color:#111}}
.msg{{background:#fff;border-radius:14px;padding:12px 14px;margin:12px 0;white-space:pre-wrap;box-shadow:0 1px 2px #0002}}
.meta{{color:#555;font-size:13px}} a{{color:#2481cc}}
</style></head><body>
<p class="meta">{meta}</p>
{parts}
</body></html>
"""


def write_preview(path: Path, parts: list[str], meta: str) -> None:
    body = "\n".join(f'<div class="msg">{p}</div>' for p in parts)
    path.write_text(PREVIEW_TEMPLATE.format(meta=meta, parts=body), encoding="utf-8")


# ------------------------------------------------------------------ сценарий

def drop_external_replies(records: list[MessageRecord]) -> list[MessageRecord]:
    """Ответы на сообщения вне выгрузки (в форумах — корень темы) модели ничего не дают."""
    known = {r.message_id for r in records}
    return [r if r.reply_to_message_id in known else replace(r, reply_to_message_id=None) for r in records]


def load_export(path: Path, chat_query: str | None, chat_id: int | None, tz: ZoneInfo):
    """Возвращает (название чата, chat_id или None, записи). Поддерживает JSON и HTML."""
    if path.is_dir():
        json_file = path / "result.json"
        path = json_file if json_file.exists() else path
    if path.is_dir() or path.suffix.lower() == ".html":
        export = parse_html_export(path, tz)
        records = []
        for m in export.messages:
            text = m.text
            if not text or m.date is None:
                continue  # медиа без подписи или неразобранная дата
            if m.forwarded_from:
                text = f"[переслано от {m.forwarded_from}] {text}"
            records.append(MessageRecord(
                chat_id=chat_id or 0, message_id=m.message_id, user_id=None, username=None,
                display_name=(m.author or "Без имени")[:128], date=m.date, text=text,
                reply_to_message_id=m.reply_to,
            ))
        records.sort(key=lambda r: (r.date, r.message_id))
        return export.chat_name, chat_id, drop_external_replies(records)

    data = json.loads(path.read_text(encoding="utf-8"))
    chat = select_chat(data, chat_query)
    chat_id = chat_id if chat_id is not None else chat_id_from_export(chat)
    return chat.get("name"), chat_id, drop_external_replies(parse_export(chat, chat_id, tz))


async def run(args) -> int:
    dry = args.dry_run or args.no_summary
    settings = build_settings(args.chat_id or 0, need_key=not dry)
    tz = ZoneInfo(args.export_tz) if args.export_tz else settings.tz
    chat_name, chat_id, records = load_export(Path(args.export), args.chat, args.chat_id, tz)

    period = PERIOD_7D if args.period == "7d" else PERIOD_24H
    end = window_end(args.end, records, tz)
    start = end - period.seconds
    window = [r for r in records if start <= r.date < end]

    print(f"Чат: {chat_name} (chat_id {chat_id if chat_id is not None else 'неизвестен — укажите --chat-id для ссылок'})")
    print(f"Текстовых сообщений в экспорте: {len(records)}"
          + (f" ({format_period(records[0].date, records[-1].date + 1, settings.tz)})" if records else ""))
    print(f"Окно: {format_period(start, end, settings.tz)} ({settings.timezone}) — сообщений: {len(window)}, "
          f"участников: {len({r.user_id or r.display_name for r in window})}")

    if args.import_db:
        if chat_id is None:
            raise SystemExit("Для импорта HTML-экспорта нужен --chat-id (узнать: /chatid в группе)")
        db = Database(args.import_db)
        db.ensure_schema()
        now = int(time.time())
        inserted = sum(db.upsert_message(r, now) for r in records)
        db.close()
        print(f"Импорт в {args.import_db}: добавлено {inserted}, уже было {len(records) - inserted}")

    if args.no_summary:
        return 0
    if not window:
        print("В окне нет сообщений. Для старого экспорта используйте --end last или --end 2026-11-20T22:00")
        return 1

    from llm_client import OpenRouterClient
    from summary_generator import SummaryGenerationError, SummaryGenerator

    if args.dry_run:
        generator = SummaryGenerator(settings, llm=None)
        lines = [generator.message_line(m) for m in window]
        total = sum(len(line) + 1 for line in lines)
        chunks = generator._chunk(lines)
        print(f"Объём для LLM: {total} символов (~{total // 2} токенов), фрагментов: {len(chunks)}, "
              f"ожидаемо вызовов LLM: {len(chunks) + (1 if len(chunks) > 1 else 0)}+"
              + (" — превышен LLM_MAX_INPUT_CHARS, будут взяты последние сообщения"
                 if total > settings.llm_max_input_chars else ""))
        if len(window) < settings.min_messages:
            print(f"Сообщений меньше MIN_MESSAGES_FOR_SUMMARY={settings.min_messages} — бот не стал бы вызывать LLM")
        return 0

    if len(window) < settings.min_messages:
        print(f"Сообщений меньше MIN_MESSAGES_FOR_SUMMARY={settings.min_messages} — бот не стал бы вызывать LLM")
        return 1

    client = OpenRouterClient(
        api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url, model=settings.llm_model,
        temperature=settings.llm_temperature, max_tokens=settings.llm_max_output_tokens,
        timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
    )
    print(f"Генерация через OpenRouter, модель {settings.llm_model}…")
    started = time.monotonic()
    try:
        result = await SummaryGenerator(settings, client).generate(window, start, end)
    except SummaryGenerationError as exc:
        print(f"Не удалось сформировать сводку: {exc}")
        return 2
    finally:
        await client.aclose()

    blocks = render_summary(
        result.payload, chat_id=chat_id or 0, heading=period.heading, ref_fallback=True,
        period_text=f"{format_period(start, end, settings.tz)} ({settings.timezone})",
        message_count=result.message_count, participants=result.participants, used_messages=result.used_messages,
    )
    parts = split_blocks(blocks)
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "н/д"
    meta = (f"Модель {result.model or settings.llm_model}; вызовов LLM: {result.llm_calls}; "
            f"токены: {result.prompt_tokens}/{result.completion_tokens}; стоимость: {cost}; "
            f"отброшено несуществующих ID: {result.dropped_ids}; время: {time.monotonic() - started:.1f} с; "
            f"сообщений Telegram: {len(parts)}")

    print("\n" + "=" * 70)
    for i, part in enumerate(parts, 1):
        if len(parts) > 1:
            print(f"--- сообщение {i}/{len(parts)} ---")
        print(strip_html(part))
    print("=" * 70)
    print(meta)
    out = Path(args.out)
    write_preview(out, parts, meta)
    print(f"HTML-превью (как в Telegram, со ссылками): {out.resolve()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", help="экспорт Telegram Desktop: result.json (JSON), messages.html или папка экспорта")
    parser.add_argument("--period", choices=["24h", "7d"], default="24h", help="длина окна (по умолчанию 24h)")
    parser.add_argument("--end", default="now",
                        help="конец окна: now (по умолчанию), last — время последнего сообщения, или ISO-дата")
    parser.add_argument("--chat", help="ID или часть названия чата (для экспорта всего аккаунта)")
    parser.add_argument("--chat-id", type=int, help="переопределить chat_id (формат Bot API, напр. -1001234567890)")
    parser.add_argument("--export-tz", help="часовой пояс дат в старых экспортах без *_unixtime (по умолчанию TIMEZONE)")
    parser.add_argument("--out", default="summary-preview.html", help="куда сохранить HTML-превью")
    parser.add_argument("--dry-run", action="store_true", help="не обращаться к LLM, только оценить объём")
    parser.add_argument("--import-db", help="загрузить сообщения экспорта в SQLite-базу бота")
    parser.add_argument("--no-summary", action="store_true", help="не формировать сводку (например, только импорт)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
