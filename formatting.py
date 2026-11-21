"""Подготовка текста сводки для Telegram (parse_mode=HTML).

Весь текст от модели и участников экранируется. Сводка собирается из
самостоятельных HTML-блоков (каждый тег открывается и закрывается внутри блока),
поэтому разбиение на сообщения по границам блоков никогда не ломает разметку.
Длина считается в UTF-16 (как в Telegram) с запасом от лимита 4096.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from summary_generator import Point, SummaryPayload

TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_MESSAGE_LIMIT = 4000


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def strip_html(text: str) -> str:
    """Текстовая версия HTML-сообщения (запасной вариант при ошибке разбора разметки)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def message_link(chat_id: int, message_id: int) -> str | None:
    """Ссылка на сообщение супергруппы: https://t.me/c/<id без -100>/<message_id>.

    Для обычных (не супер-) групп Telegram не поддерживает ссылки на сообщения.
    """
    raw = str(chat_id)
    if not raw.startswith("-100") or len(raw) <= 4:
        return None
    return f"https://t.me/c/{raw[4:]}/{int(message_id)}"


@dataclass(frozen=True)
class Block:
    html: str
    plain: str
    gap_before: bool = False  # пустая строка перед блоком (начало раздела)


def _links(chat_id: int, ids: list[int], ref_fallback: bool = False) -> str:
    """Ссылки на сообщения; если ссылку построить нельзя и ref_fallback — номер сообщения (#123)."""
    links = []
    for index, mid in enumerate(ids, start=1):
        url = message_link(chat_id, mid)
        if url:
            links.append(f'<a href="{html.escape(url, quote=True)}">[{index}]</a>')
        elif ref_fallback:
            links.append(f"<code>#{int(mid)}</code>")
    return (" " + " ".join(links)) if links else ""


def _block(html_text: str, gap: bool = False) -> Block:
    return Block(html=html_text, plain=strip_html(html_text), gap_before=gap)


def _point_blocks(
    title: str, points: list[Point], chat_id: int, empty_text: str, ref_fallback: bool = False
) -> list[Block]:
    header = f"<b>{esc(title)}</b>"
    if not points:
        return [_block(f"{header}\n<i>{esc(empty_text)}</i>", gap=True)]
    blocks = []
    for i, point in enumerate(points):
        item = f"• {esc(point.text)}{_links(chat_id, point.message_ids, ref_fallback)}"
        blocks.append(_block(f"{header}\n{item}" if i == 0 else item, gap=(i == 0)))
    return blocks


def format_period(start_ts: int, end_ts: int, tz: ZoneInfo) -> str:
    start = datetime.fromtimestamp(start_ts, timezone.utc).astimezone(tz)
    end = datetime.fromtimestamp(end_ts, timezone.utc).astimezone(tz)
    return f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}"


def render_summary(
    payload: SummaryPayload,
    *,
    chat_id: int,
    heading: str,
    period_text: str,
    message_count: int,
    participants: int,
    used_messages: int | None = None,
    ref_fallback: bool = False,
) -> list[Block]:
    blocks: list[Block] = [
        _block(
            f"<b>📋 {esc(heading)}</b>\n"
            f"<i>{esc(period_text)} · {message_count} {plural(message_count, 'сообщение', 'сообщения', 'сообщений')}"
            f" · {participants} {plural(participants, 'участник', 'участника', 'участников')}</i>"
        )
    ]
    if used_messages is not None and used_messages < message_count:
        blocks.append(_block(
            f"<i>⚠️ Переписка слишком объёмная: в анализ вошли последние {used_messages} "
            f"из {message_count} сообщений.</i>"
        ))

    topics_header = "<b>Основные темы</b>"
    if payload.topics:
        for i, topic in enumerate(payload.topics):
            item = f"🔹 <b>{esc(topic.title)}</b>"
            if topic.summary:
                item += f" — {esc(topic.summary)}"
            item += _links(chat_id, topic.message_ids, ref_fallback)
            blocks.append(_block(f"{topics_header}\n{item}" if i == 0 else item, gap=(i == 0)))
    else:
        blocks.append(_block(f"{topics_header}\n<i>Содержательных обсуждений не выявлено.</i>", gap=True))

    blocks += _point_blocks("Принятые решения", payload.decisions, chat_id, "Решения не зафиксированы.", ref_fallback)
    blocks += _point_blocks(
        "Незакрытые вопросы", payload.open_questions, chat_id, "Открытых вопросов не выявлено.", ref_fallback
    )

    overall = payload.overall or "Без итогового резюме."
    blocks.append(_block(f"<b>Итог</b>\n{esc(overall)}", gap=True))
    return blocks


def render_notice(text: str) -> list[Block]:
    return [_block(esc(text))]


def _split_plain(text: str, limit: int) -> list[str]:
    """Режет длинный текст по словам так, чтобы экранированные куски укладывались в лимит."""
    pieces: list[str] = []
    current = ""
    for word in re.split(r"(\s+)", text):
        candidate = current + word
        if current and utf16_len(esc(candidate)) > limit:
            pieces.append(current.strip())
            current = word.lstrip()
        else:
            current = candidate
        while utf16_len(esc(current)) > limit:  # одно «слово» длиннее лимита
            cut = max(1, limit // 6)  # экранирование может увеличить длину до 5 раз (&amp;)
            pieces.append(current[:cut])
            current = current[cut:]
    if current.strip():
        pieces.append(current.strip())
    return pieces


def split_blocks(blocks: list[Block], limit: int = SAFE_MESSAGE_LIMIT) -> list[str]:
    """Упаковывает блоки в сообщения не длиннее limit (в UTF-16)."""
    normalized: list[Block] = []
    for block in blocks:
        if utf16_len(block.html) <= limit:
            normalized.append(block)
        else:
            for j, piece in enumerate(_split_plain(block.plain, limit)):
                normalized.append(Block(html=esc(piece), plain=piece, gap_before=block.gap_before and j == 0))

    messages: list[str] = []
    current = ""
    for block in normalized:
        separator = "\n\n" if block.gap_before else "\n"
        candidate = f"{current}{separator}{block.html}" if current else block.html
        if current and utf16_len(candidate) > limit:
            messages.append(current)
            current = block.html
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
