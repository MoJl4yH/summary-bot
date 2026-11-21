"""Общий тестовый стенд: подменные OpenRouter и Telegram, управляемые часы.

Реальные ключи и сеть не используются: OpenRouter подменяется через
httpx.MockTransport, Telegram Bot — объектом FakeBot с тем же интерфейсом
send_message(**kwargs).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from telegram import Chat, Message, MessageEntity, Update, User

from bot import BotApp
from config import load_settings
from database import Database, MessageRecord
from llm_client import OpenRouterClient
from scheduler import Maintenance, Scheduler
from service import SummaryService
from summary_generator import SummaryGenerator
from telegram_io import TelegramPublisher

CHAT_A = -1001000000001
CHAT_B = -1001000000002
CHAT_FOREIGN = -1009999999999
USER_ALICE = 101
USER_BOB = 202
USER_MALLORY = 666

# Суббота 21.11.2026 20:00 UTC = 23:00 по Москве
T0 = int(datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc).timestamp())


class Clock:
    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def summary_payload(ids: list[int], title: str = "Обсуждение релиза") -> dict:
    return {
        "topics": [{"title": title, "summary": "Участники обсудили сроки.", "message_ids": ids[:2]}],
        "decisions": [{"text": "Релиз в пятницу", "message_ids": ids[-1:]}],
        "open_questions": [],
        "overall": "Продуктивный день.",
    }


def llm_response(content: str | dict, *, cost: float | None = 0.001, status: int = 200, headers=None,
                 finish_reason: str = "stop") -> httpx.Response:
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    body = {
        "id": "gen-test",
        "model": "test/model",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": cost},
    }
    return httpx.Response(status, json=body, headers=headers or {})


def ids_in_request(body: dict) -> list[int]:
    user = body["messages"][-1]["content"]
    return [int(x) for x in re.findall(r'"id": (\d+)', user)] or [
        int(x) for x in re.findall(r'"message_ids": \[(\d+)', user)
    ]


class FakeOpenRouter:
    """Очередь заготовленных ответов; если очередь пуста — корректная сводка по
    идентификаторам из запроса."""

    def __init__(self):
        self.queue: list = []
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def push(self, *items) -> None:
        self.queue.extend(items)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        if self.queue:
            item = self.queue.pop(0)
            if callable(item):
                item = item(body)
            if isinstance(item, Exception):
                raise item
            return item
        return llm_response(summary_payload(ids_in_request(body)))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def user_contents(self) -> list[str]:
        return [r["messages"][-1]["content"] for r in self.requests]


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.errors: list = []
        self._next_id = 5000

    async def send_message(self, **kwargs):
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self._next_id += 1
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=self._next_id, delete=_async_noop)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


async def _async_noop(*args, **kwargs):
    return True


async def no_sleep(_seconds: float) -> None:
    return None


def make_env(tmp_path, **overrides) -> dict:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:AAEtesttokenvalue_abcdefghijklmnopqrs",
        "TELEGRAM_GROUP_CHAT_IDS": f"{CHAT_A},{CHAT_B}",
        "OPENROUTER_API_KEY": "sk-or-v1-testkey0123456789",
        "TIMEZONE": "Europe/Moscow",
        "DATABASE_PATH": str(tmp_path / "bot.db"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "MIN_MESSAGES_FOR_SUMMARY": "2",
        "DAILY_SUMMARY_TIME": "22:00",
        "WEEKLY_SUMMARY_DAY": "sunday",
        "WEEKLY_SUMMARY_TIME": "23:00",
        "LLM_MAX_RETRIES": "3",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return env


@dataclass
class Harness:
    settings: object
    db: Database
    clock: Clock
    llm: FakeOpenRouter
    bot: FakeBot
    client: OpenRouterClient
    service: SummaryService
    scheduler: Scheduler
    maintenance: Maintenance
    app: BotApp
    sleeps: list = field(default_factory=list)

    def add(self, chat_id: int, message_id: int, ago: int, text: str = "сообщение", *,
            user_id: int = USER_ALICE, name: str = "Алиса") -> None:
        self.db.upsert_message(
            MessageRecord(chat_id=chat_id, message_id=message_id, user_id=user_id, username=None,
                          display_name=name, date=int(self.clock()) - ago, text=text),
            int(self.clock()),
        )

    async def close(self) -> None:
        await self.client.aclose()
        self.db.close()


def build_harness(tmp_path, *, clock: Clock | None = None, llm: FakeOpenRouter | None = None,
                  bot: FakeBot | None = None, init: bool = True, **env) -> Harness:
    settings = load_settings(make_env(tmp_path, **env))
    clock = clock or Clock(T0)
    llm = llm or FakeOpenRouter()
    bot = bot or FakeBot()
    sleeps: list = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    db = Database(settings.database_path)
    if init:
        db.initialize(int(clock()))
    client = OpenRouterClient(
        api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url, model=settings.llm_model,
        temperature=settings.llm_temperature, max_tokens=settings.llm_max_output_tokens,
        timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
        transport=llm.transport, sleep=record_sleep,
    )
    publisher = TelegramPublisher(bot, sleep=record_sleep)
    service = SummaryService(settings, db, SummaryGenerator(settings, client), publisher, clock=clock)
    return Harness(
        settings=settings, db=db, clock=clock, llm=llm, bot=bot, client=client, service=service,
        scheduler=Scheduler(settings, db, service, clock=clock),
        maintenance=Maintenance(settings, db, clock=clock),
        app=BotApp(settings, db, service, clock=clock), sleeps=sleeps,
    )


@pytest.fixture
async def h(tmp_path):
    harness = build_harness(tmp_path)
    yield harness
    await harness.close()


# --------------------------------------------------------------- Telegram-объекты

def tg_message(chat_id: int, message_id: int, *, user_id: int = USER_ALICE, text: str | None = "текст",
               caption: str | None = None, date: datetime | None = None, chat_type: str = "supergroup",
               is_bot: bool = False, bot: FakeBot | None = None, reply_to: Message | None = None,
               edit_date: datetime | None = None, thread_id: int | None = None,
               is_topic: bool | None = None) -> Message:
    chat = Chat(id=chat_id, type=chat_type, title="Тестовая группа")
    user = User(id=user_id, first_name="Иван", last_name="Петров", is_bot=is_bot, username="ivan")
    message = Message(
        message_id=message_id, date=date or datetime.fromtimestamp(T0, timezone.utc), chat=chat,
        from_user=user, text=text, caption=caption, reply_to_message=reply_to, edit_date=edit_date,
        message_thread_id=thread_id, is_topic_message=is_topic,
        entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))]
        if text and text.startswith("/") else None,
    )
    if bot is not None:
        message.set_bot(bot)
    return message


def command_update(chat_id: int, user_id: int, text: str = "/summary", *, bot: FakeBot,
                   chat_type: str = "supergroup", message_id: int = 900) -> Update:
    message = tg_message(chat_id, message_id, user_id=user_id, text=text, chat_type=chat_type, bot=bot)
    return Update(update_id=message_id, message=message)


def context(*args: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))
