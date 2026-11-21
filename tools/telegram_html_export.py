"""Разбор HTML-экспорта чата Telegram Desktop (messages.html, messages2.html, ...).

Особенности формата:
  * дата — в атрибуте title: «21 ноября 2026, 20:39:36» или «21.11.2026 20:39:36 UTC+03:00»;
    без смещения это локальное время компьютера, на котором делали экспорт;
  * подряд идущие сообщения одного автора помечены классом joined и не содержат имени;
  * ID чата и пользователей в HTML не сохраняются;
  * в форумах почти каждое сообщение «отвечает» на корневое сообщение темы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
    "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_NUMERIC = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s+UTC([+-]\d{1,2}):?(\d{2})?)?"
)
_WORDY = re.compile(r"(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})(?:\s*г\.)?,?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_title_date(title: str, tz: ZoneInfo) -> int | None:
    title = title.strip()
    if m := _NUMERIC.match(title):
        day, month, year, hour, minute, second, off_h, off_m = m.groups()
        if off_h is not None:
            sign = -1 if off_h.startswith("-") else 1
            offset = timedelta(hours=abs(int(off_h)), minutes=int(off_m or 0)) * sign
            zone = timezone(offset)
        else:
            zone = tz
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=zone)
        return int(dt.timestamp())
    if m := _WORDY.match(title):
        day, month_name, year, hour, minute, second = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            dt = datetime(int(year), month, int(day), int(hour), int(minute), int(second or 0), tzinfo=tz)
            return int(dt.timestamp())
    return None


@dataclass
class HtmlMessage:
    message_id: int
    date: int | None = None
    author: str | None = None
    joined: bool = False
    reply_to: int | None = None
    text_parts: list[str] = field(default_factory=list)
    forwarded_from: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(p for p in ("".join(self.text_parts).strip(),) if p)


class _Parser(HTMLParser):
    def __init__(self, tz: ZoneInfo):
        super().__init__(convert_charrefs=True)
        self.tz = tz
        self.messages: list[HtmlMessage] = []
        self.chat_name: str | None = None
        self._stack: list[set[str]] = []        # классы открытых <div>
        self._message: HtmlMessage | None = None
        self._message_depth = -1
        self._capture: str | None = None        # что сейчас собираем: text/from_name/fwd_name/chat_name
        self._capture_depth = -1
        self._buffer: list[str] = []
        self._forwarded_depth = -1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br" and self._capture:
            self._buffer.append("\n")
            return
        if tag == "a" and self._message and "reply_to" in self._open_classes():
            match = re.search(r"go_to_message(\d+)", attrs.get("href") or "") or re.search(
                r"GoToMessage\((\d+)\)", attrs.get("onclick") or "")
            if match:
                self._message.reply_to = int(match.group(1))
            return
        if tag != "div":
            return
        classes = set((attrs.get("class") or "").split())
        self._stack.append(classes)
        depth = len(self._stack)

        if "message" in classes:
            element_id = attrs.get("id") or ""
            mid = element_id.removeprefix("message")
            if "default" in classes and mid.isdigit():
                self._message = HtmlMessage(int(mid), joined="joined" in classes)
                self._message_depth = depth
            return
        if self._capture:
            return
        if self.chat_name is None and self._message is None and "bold" in classes and classes & {"name", "text"}:
            self._start_capture("chat_name", depth)
        elif self._message is None:
            return
        elif "forwarded" in classes and "body" in classes:
            self._forwarded_depth = depth
        elif "date" in classes and "details" in classes and self._message.date is None and attrs.get("title"):
            self._message.date = parse_title_date(attrs["title"], self.tz)
        elif "from_name" in classes:
            self._start_capture("fwd_name" if self._forwarded_depth > 0 else "from_name", depth)
        elif classes == {"text"}:
            self._start_capture("text", depth)

    def _open_classes(self) -> set[str]:
        return self._stack[-1] if self._stack else set()

    def _start_capture(self, kind: str, depth: int) -> None:
        self._capture, self._capture_depth, self._buffer = kind, depth, []

    def handle_data(self, data):
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag != "div" or not self._stack:
            return
        depth = len(self._stack)
        if self._capture and depth == self._capture_depth:
            value = "".join(self._buffer)
            if self._capture == "chat_name":
                self.chat_name = value.strip()
            elif self._message is not None:
                if self._capture == "from_name":
                    self._message.author = value.strip()
                elif self._capture == "fwd_name":
                    self._message.forwarded_from = value.strip().split("\n")[0].strip()
                elif self._capture == "text":
                    self._message.text_parts.append(value)
            self._capture = None
        if depth == self._forwarded_depth:
            self._forwarded_depth = -1
        if self._message is not None and depth == self._message_depth:
            self.messages.append(self._message)
            self._message, self._message_depth = None, -1
        self._stack.pop()


def _file_order(path: Path) -> int:
    match = re.fullmatch(r"messages(\d*)\.html", path.name)
    return int(match.group(1) or 1) if match else 0


def find_html_files(path: Path) -> list[Path]:
    directory = path if path.is_dir() else path.parent
    return sorted(directory.glob("messages*.html"), key=_file_order)


@dataclass
class HtmlExport:
    chat_name: str | None
    messages: list[HtmlMessage]


def parse_html_export(path: Path, tz: ZoneInfo) -> HtmlExport:
    files = find_html_files(path)
    if not files:
        raise SystemExit(f"Не найдены файлы messages*.html в {path}")
    chat_name = None
    messages: list[HtmlMessage] = []
    for file in files:
        parser = _Parser(tz)
        parser.feed(file.read_text(encoding="utf-8"))
        parser.close()
        chat_name = chat_name or parser.chat_name
        messages.extend(parser.messages)

    # Автор «склеенных» сообщений — как у предыдущего
    last_author = None
    for message in messages:
        if message.author:
            last_author = message.author
        elif last_author:
            message.author = last_author
    return HtmlExport(chat_name, messages)
