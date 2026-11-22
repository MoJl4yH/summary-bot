"""Сохранение сообщений: запись, дубликаты, изоляция групп, правки, миграция, срок хранения."""
import sqlite3
from datetime import datetime, timezone

from telegram import Chat, Message, Update

from bot import extract_record
from conftest import CHAT_A, CHAT_B, CHAT_FOREIGN, T0, context, tg_message
from database import Database


async def test_incoming_message_is_saved_with_all_fields(h):
    """Сценарий 1: сохранение входящего сообщения."""
    original = tg_message(CHAT_A, 10, text="Исходное")
    reply = tg_message(CHAT_A, 11, text="Ответ", reply_to=original, thread_id=4, is_topic=True,
                       date=datetime.fromtimestamp(T0 - 60, timezone.utc))
    await h.app.on_message(Update(update_id=1, message=reply), context())

    [saved] = h.db.get_messages(CHAT_A, 0, T0 + 1)
    assert saved.chat_id == CHAT_A
    assert saved.message_id == 11
    assert saved.user_id == 101
    assert saved.username == "ivan"
    assert saved.display_name == "Иван Петров"
    assert saved.date == T0 - 60  # время отправки из Telegram, а не время получения
    assert saved.text == "Ответ"
    assert saved.reply_to_message_id == 10
    assert saved.message_thread_id == 4


async def test_media_caption_is_saved_and_empty_media_ignored(h):
    await h.app.on_message(Update(update_id=1, message=tg_message(CHAT_A, 1, text=None, caption="Фото схемы")), context())
    await h.app.on_message(Update(update_id=2, message=tg_message(CHAT_A, 2, text=None)), context())
    assert [m.text for m in h.db.get_messages(CHAT_A, 0, T0 + 1)] == ["Фото схемы"]


async def test_duplicate_update_is_stored_once(h):
    """Сценарий 2: повторная доставка того же обновления не создаёт дубликат."""
    message = tg_message(CHAT_A, 42, text="Один раз")
    for update_id in (1, 1, 2):
        await h.app.on_message(Update(update_id=update_id, message=message), context())
    assert h.db.count_messages(CHAT_A) == 1


async def test_edited_message_updates_text(h):
    await h.app.on_message(Update(update_id=1, message=tg_message(CHAT_A, 7, text="черновик")), context())
    edited = tg_message(CHAT_A, 7, text="исправлено", edit_date=datetime.fromtimestamp(T0 + 30, timezone.utc))
    await h.app.on_message(Update(update_id=2, edited_message=edited), context())
    [saved] = h.db.get_messages(CHAT_A, 0, T0 + 1)
    assert saved.text == "исправлено"
    assert h.db.count_messages(CHAT_A) == 1


async def test_groups_are_isolated(h):
    """Сценарий 3: сообщения разных групп не смешиваются, в том числе при одинаковых message_id."""
    await h.app.on_message(Update(update_id=1, message=tg_message(CHAT_A, 1, text="секрет группы A")), context())
    await h.app.on_message(Update(update_id=2, message=tg_message(CHAT_B, 1, text="секрет группы B")), context())
    a = h.db.get_messages(CHAT_A, 0, T0 + 1)
    b = h.db.get_messages(CHAT_B, 0, T0 + 1)
    assert [m.text for m in a] == ["секрет группы A"]
    assert [m.text for m in b] == ["секрет группы B"]

    # и в запрос к LLM для группы A не попадает текст группы B
    h.add(CHAT_A, 2, 10, "ещё сообщение в A")
    await h.service.run_manual(_req(CHAT_A))
    assert all("группы B" not in c for c in h.llm.user_contents())


async def test_messages_from_foreign_group_and_private_chats_are_ignored(h):
    await h.app.on_message(Update(update_id=1, message=tg_message(CHAT_FOREIGN, 1)), context())
    await h.app.on_message(Update(update_id=2, message=tg_message(555, 2, chat_type="private")), context())
    assert h.db.count_messages(CHAT_FOREIGN) == 0
    assert h.db.count_messages(555) == 0


def test_bot_messages_skipped_and_anonymous_admin_kept():
    assert extract_record(tg_message(CHAT_A, 1, is_bot=True)) is None
    anon = Message(
        message_id=2, date=datetime.fromtimestamp(T0, timezone.utc),
        chat=Chat(id=CHAT_A, type="supergroup", title="Группа"),
        sender_chat=Chat(id=CHAT_A, type="supergroup", title="Группа"), text="от администратора",
    )
    record = extract_record(anon)
    assert record is not None and record.user_id is None and record.display_name == "Группа"


def test_forum_topic_root_is_not_treated_as_reply():
    root = tg_message(CHAT_A, 100, text="тема")
    message = tg_message(CHAT_A, 105, reply_to=root, thread_id=100, is_topic=True)
    record = extract_record(message)
    assert record.reply_to_message_id is None
    assert record.message_thread_id == 100


async def test_retention_deletes_only_expired_messages(h):
    h.add(CHAT_A, 1, 31 * 86400, "старое")
    h.add(CHAT_A, 2, 29 * 86400, "свежее")
    result = h.maintenance.run_once()
    assert result["deleted_messages"] == 1
    assert [m.text for m in h.db.get_messages(CHAT_A, 0, T0 + 1)] == ["свежее"]
    assert result["backup"] and sqlite3.connect(result["backup"]).execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0] == 1


def test_wal_mode_and_schema(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    db.initialize(T0)
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    indexes = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_messages_chat_date" in indexes
    db.close()


def test_legacy_daily_logs_are_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE daily_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL, user_name TEXT NOT NULL, message_id TEXT NOT NULL,
            content TEXT NOT NULL, reply_to_message_id TEXT, created_at TEXT NOT NULL);
        INSERT INTO daily_logs (chat_id, user_id, user_name, message_id, content, reply_to_message_id, created_at)
        VALUES ('-1001000000001', '5', 'Олег', '77', 'старое сообщение', NULL, '2026-11-21 10:00:00'),
               ('-1001000000001', '5', 'Олег', '77', 'дубль', NULL, '2026-11-21 10:00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(str(path))
    db.initialize(T0)
    [m] = db.get_messages(CHAT_A, 0, T0 * 2)
    assert (m.message_id, m.user_id, m.display_name, m.text) == (77, 5, "Олег", "старое сообщение")
    assert m.date == int(datetime(2026, 11, 21, 10, tzinfo=timezone.utc).timestamp())
    assert db.conn.execute("SELECT name FROM sqlite_master WHERE name='daily_logs'").fetchone() is None
    db.close()


def _req(chat_id):
    from service import PERIOD_24H, ManualRequest

    return ManualRequest(chat_id=chat_id, user_id=101, period=PERIOD_24H)


def test_thread_id_stored_only_for_forum_topic_messages():
    root = tg_message(CHAT_A, 50, text="корень цепочки")
    plain_reply = tg_message(CHAT_A, 51, reply_to=root, thread_id=50, is_topic=None)  # ответ вне темы
    topic_msg = tg_message(CHAT_A, 52, thread_id=4, is_topic=True)
    assert extract_record(plain_reply).message_thread_id is None
    assert extract_record(plain_reply).reply_to_message_id == 50
    assert extract_record(topic_msg).message_thread_id == 4


def test_fill_thread_ids_only_fills_missing(h):
    from database import MessageRecord

    h.db.upsert_message(MessageRecord(CHAT_A, 1, 1, None, "A", T0, "x"), T0)
    h.db.upsert_message(MessageRecord(CHAT_A, 2, 1, None, "A", T0, "y", message_thread_id=9), T0)
    assert h.db.fill_thread_ids([(4, CHAT_A, 1), (4, CHAT_A, 2), (4, CHAT_A, 3)]) == 1
    threads = {m.message_id: m.message_thread_id for m in h.db.get_messages(CHAT_A, 0, T0 + 1)}
    assert threads == {1: 4, 2: 9}  # собранное ботом значение не перезаписано
