"""Утилита tools/summarize_export.py: разбор экспорта Telegram Desktop и прогон генерации."""
import json
import sys
from argparse import Namespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeOpenRouter
from database import Database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import summarize_export as tool  # noqa: E402

NOW = 1795000000


def export_sample() -> dict:
    return {
        "name": "Команда проекта",
        "type": "private_supergroup",
        "id": 1234567890,
        "messages": [
            {"id": 1, "type": "service", "date": "2026-11-20T10:00:00", "date_unixtime": str(NOW - 90000),
             "actor": "Иван", "action": "invite_members"},
            {"id": 2, "type": "message", "date_unixtime": str(NOW - 90000), "from": "Иван Петров",
             "from_id": "user111", "text": "Старое сообщение вне окна"},
            {"id": 3, "type": "message", "date_unixtime": str(NOW - 3600), "from": "Иван Петров",
             "from_id": "user111", "text": ["Предлагаю ", {"type": "bold", "text": "релиз"}, " в пятницу"]},
            {"id": 4, "type": "message", "date_unixtime": str(NOW - 1800), "from": "Мария",
             "from_id": "user222", "reply_to_message_id": 3, "text": "Согласна, делаем",
             "edited_unixtime": str(NOW - 1700)},
            {"id": 5, "type": "message", "date_unixtime": str(NOW - 900), "from": "Мария",
             "from_id": "user222", "photo": "photos/1.jpg", "text": ""},
            {"id": 6, "type": "message", "date_unixtime": str(NOW - 600), "from": "Канал новостей",
             "from_id": "channel999", "text": "Подпись к видео"},
        ],
    }


def test_parse_export_formats():
    chat = export_sample()
    chat_id = tool.chat_id_from_export(chat)
    assert chat_id == -1001234567890
    records = tool.parse_export(chat, chat_id, ZoneInfo("Europe/Moscow"))
    assert [r.message_id for r in records] == [2, 3, 4, 6]  # служебные и медиа без подписи пропущены
    assert records[1].text == "Предлагаю релиз в пятницу"
    assert (records[2].user_id, records[2].reply_to_message_id, records[2].edit_date) == (222, 3, NOW - 1700)
    assert records[3].user_id is None and records[3].display_name == "Канал новостей"


def test_old_export_without_unixtime_uses_timezone():
    chat = {"id": 5, "type": "private_group", "messages": [
        {"id": 1, "type": "message", "date": "2026-11-21T22:00:00", "from": "A", "from_id": "user1", "text": "x"}]}
    assert tool.chat_id_from_export(chat) == -5
    [record] = tool.parse_export(chat, -5, ZoneInfo("Europe/Moscow"))
    assert record.date == 1795287600  # 22:00 МСК = 19:00 UTC


def test_account_export_requires_chat_choice():
    data = {"chats": {"list": [export_sample(), {**export_sample(), "id": 42, "name": "Другой"}]}}
    with pytest.raises(SystemExit, match="несколько чатов"):
        tool.select_chat(data, None)
    assert tool.select_chat(data, "Другой")["id"] == 42
    assert tool.select_chat(data, "-1001234567890")["name"] == "Команда проекта"


async def test_tool_generates_preview_and_imports(tmp_path, monkeypatch, capsys):
    export = tmp_path / "result.json"
    export.write_text(json.dumps(export_sample(), ensure_ascii=False), encoding="utf-8")
    fake = FakeOpenRouter()
    import llm_client

    original = llm_client.OpenRouterClient
    monkeypatch.setattr(llm_client, "OpenRouterClient", lambda **kw: original(**kw, transport=fake.transport))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey0123456789")
    monkeypatch.setenv("MIN_MESSAGES_FOR_SUMMARY", "2")
    db_path = tmp_path / "bot.db"
    args = Namespace(export=str(export), chat=None, chat_id=None, export_tz=None, period="24h", end="last",
                     out=str(tmp_path / "preview.html"), dry_run=False, import_db=str(db_path), no_summary=False, thread=None)

    assert await tool.run(args) == 0
    out = capsys.readouterr().out
    assert "сообщений: 3" in out and "Сводка за 24 часа" in out
    assert len(fake.requests) == 1
    sent = fake.user_contents()[0]
    assert '"id": 3' in sent and '"id": 2' not in sent  # окно 24 ч от последнего сообщения
    html = (tmp_path / "preview.html").read_text(encoding="utf-8")
    assert 'href="https://t.me/c/1234567890/' in html
    db = Database(str(db_path))
    assert db.count_messages(-1001234567890) == 4
    db.close()


async def test_tool_dry_run_needs_no_key(tmp_path, monkeypatch, capsys):
    export = tmp_path / "result.json"
    export.write_text(json.dumps(export_sample(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    args = Namespace(export=str(export), chat=None, chat_id=None, export_tz=None, period="7d", end="last",
                     out=str(tmp_path / "p.html"), dry_run=True, import_db=None, no_summary=False, thread=None)
    assert await tool.run(args) == 0
    assert "Объём для LLM" in capsys.readouterr().out


HTML_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"/></head><body><div class="page_wrap">
<div class="page_header chat_header"><div class="content bubble"><div class="body">
<div class="name bold">Тестовая &amp; группа</div></div></div></div>
<div class="page_body chat_page"><div class="history">
<div class="message service" id="message-1"><div class="body details">21 ноября 2026</div></div>
{messages}
</div></div></div></body></html>"""


def html_message(mid, title, text=None, author=None, joined=False, reply_to=None, media=False):
    parts = [f'<div class="message default clearfix{" joined" if joined else ""}" id="message{mid}">',
             '<div class="body">', f'<div class="pull_right date details" title="{title}">20:00</div>']
    if author:
        parts.append(f'<div class="from_name">\n{author}\n</div>')
    if reply_to:
        parts.append(f'<div class="reply_to details">In reply to <a href="#go_to_message{reply_to}" '
                     f'onclick="return GoToMessage({reply_to})">this message</a></div>')
    if media:
        parts.append('<div class="media_wrap clearfix"><div class="title bold">Photo</div></div>')
    if text is not None:
        parts.append(f'<div class="text">\n{text}\n</div>')
    parts.append('<div class="reactions"><span class="reaction"><span class="emoji">❤</span>'
                 '<span class="count">3</span></span></div></div></div>')
    return "\n".join(parts)


def test_html_export_parsing(tmp_path, monkeypatch, capsys):
    page1 = HTML_PAGE.format(messages="\n".join([
        html_message(10, "21 ноября 2026, 20:00:00", "Первое<br>вторая строка &lt;тег&gt;", author="Иван", reply_to=4),
        html_message(11, "21 ноября 2026, 20:01:00", "Продолжение", joined=True),
        html_message(12, "21 ноября 2026, 20:02:00", None, author="Мария", media=True),
    ]))
    page2 = HTML_PAGE.format(messages="\n".join([
        html_message(13, "21.11.2026 20:03:00 UTC+03:00", "Подпись к фото", author="Мария", media=True,
                     reply_to=10),
    ]))
    (tmp_path / "messages.html").write_text(page1, encoding="utf-8")
    (tmp_path / "messages2.html").write_text(page2, encoding="utf-8")

    name, chat_id, records = tool.load_export(tmp_path, None, None, ZoneInfo("Europe/Moscow"))
    assert name == "Тестовая & группа" and chat_id is None
    assert [r.message_id for r in records] == [10, 11, 13]  # медиа без подписи пропущено
    first, joined, caption = records
    assert first.text == "Первое\nвторая строка <тег>"
    assert first.reply_to_message_id is None  # ответ на корень темы вне выгрузки отброшен
    assert first.date == 1795280400  # 20:00 МСК = 17:00 UTC
    assert joined.display_name == "Иван"  # автор «склеенного» сообщения унаследован
    assert caption.reply_to_message_id == 10 and caption.date == first.date + 180

    fake = FakeOpenRouter()
    import llm_client

    original = llm_client.OpenRouterClient
    monkeypatch.setattr(llm_client, "OpenRouterClient", lambda **kw: original(**kw, transport=fake.transport))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey0123456789")
    monkeypatch.setenv("MIN_MESSAGES_FOR_SUMMARY", "2")
    args = Namespace(export=str(tmp_path / "messages.html"), chat=None, chat_id=None, export_tz=None, period="24h",
                     end="last", out=str(tmp_path / "p.html"), dry_run=False, import_db=None, no_summary=False, thread=None)
    assert await_run(args) == 0
    assert "<code>#10</code>" in (tmp_path / "p.html").read_text(encoding="utf-8")  # без chat_id — номера


def await_run(args):
    import asyncio

    return asyncio.run(tool.run(args))


def test_infer_topics_from_reply_chains():
    from database import MessageRecord

    def rec(mid, reply=None):
        return MessageRecord(-100, mid, None, None, "A", 1000 + mid, "t", reply_to_message_id=reply)

    records = tool.infer_topics([
        rec(10, reply=4),      # ответ на корень темы 4 (корня нет в выгрузке)
        rec(11, reply=10),     # ответ внутри темы 4
        rec(12, reply=11),     # глубже по цепочке
        rec(13),               # без ответа — General
        rec(14, reply=13),     # ответ в General
        rec(15, reply=71168),  # другая тема
    ])
    assert [r.message_thread_id for r in records] == [4, 4, 4, None, None, 71168]


async def test_reimport_fills_topics_for_existing_rows(tmp_path, monkeypatch):
    export = tmp_path / "result.json"
    data = export_sample()
    data["messages"].append({"id": 7, "type": "message", "date_unixtime": str(NOW - 300), "from": "Мария",
                             "from_id": "user222", "reply_to_message_id": 4, "text": "в той же теме"})
    for m in data["messages"]:
        if m["id"] == 3:
            m["reply_to_message_id"] = 1000  # корень темы 1000 вне выгрузки
    export.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    db_path = tmp_path / "bot.db"
    db = Database(str(db_path))
    db.ensure_schema()
    from database import MessageRecord
    db.upsert_message(MessageRecord(-1001234567890, 3, 111, None, "Иван", NOW - 3600, "старый импорт"), NOW)
    db.close()

    args = Namespace(export=str(export), chat=None, chat_id=None, export_tz=None, period="24h", end="last",
                     out=str(tmp_path / "p.html"), dry_run=False, import_db=str(db_path), no_summary=True,
                     thread=None)
    assert await tool.run(args) == 0
    db = Database(str(db_path))
    threads = {m.message_id: m.message_thread_id for m in db.get_messages(-1001234567890, 0, NOW * 2)}
    db.close()
    assert threads[3] == 1000 and threads[4] == 1000 and threads[7] == 1000  # 4 отвечает на 3 → тема 1000
    assert threads[2] is None and threads[6] is None
