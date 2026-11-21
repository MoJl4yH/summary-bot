"""Telegram-бот сводок обсуждений группового чата.

Собирает текстовые сообщения и подписи к медиа из разрешённых групп (Bot API,
long polling), хранит их в SQLite и публикует сводки, сформированные через
OpenRouter: ежедневно, еженедельно и по команде /summary с ограничением частоты.
Бот работает как обычный участник группы (без прав администратора).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path

from telegram import BotCommand, LinkPreviewOptions, Message, Update
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import APP_VERSION, WEEKDAY_NAMES_RU, ConfigError, Settings, load_settings

WEEKDAYS_ACCUSATIVE_RU = (
    "по понедельникам", "по вторникам", "по средам", "по четвергам",
    "по пятницам", "по субботам", "по воскресеньям",
)
from database import Database, MessageRecord
from formatting import esc
from llm_client import OpenRouterClient
from logging_setup import setup_logging
from scheduler import Maintenance, Scheduler, next_daily_slot, next_weekly_slot
from service import DAY, KIND_RU, ManualRequest, SummaryService, parse_period
from summary_generator import SummaryGenerator
from telegram_io import TelegramPublisher

logger = logging.getLogger("summary_bot")

GROUP_TYPES = ("group", "supergroup")
ANONYMOUS_ADMIN_ID = 1087968824  # служебный пользователь @GroupAnonymousBot
NOTICE_THROTTLE_SECONDS = 30
STATUS_RU = {
    "generating": "формируется", "ready": "сформирована, ожидает отправки", "sent": "опубликована",
    "failed": "ошибка", "empty": "мало сообщений, LLM не вызывался",
}



def extract_record(message: Message, *, ignore_bots: bool = True) -> MessageRecord | None:
    """Преобразует сообщение Telegram в запись для хранения (или None, если сохранять нечего)."""
    text = message.text or message.caption
    if not text or not text.strip():
        return None
    user = message.from_user
    sender_chat = message.sender_chat
    if sender_chat is not None:
        # Анонимный администратор, канал или автоперепост из связанного канала
        user_id = None
        username = sender_chat.username
        display_name = sender_chat.title or sender_chat.username or "Канал"
    elif user is not None:
        if user.is_bot and ignore_bots:
            return None
        user_id = user.id
        username = user.username
        display_name = user.full_name or user.username or str(user.id)
    else:
        return None

    reply_to = None
    if message.reply_to_message is not None:
        reply_to = message.reply_to_message.message_id
        # В форумах каждое сообщение темы формально «отвечает» на её служебное сообщение
        if message.is_topic_message and reply_to == message.message_thread_id:
            reply_to = None

    return MessageRecord(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=user_id,
        username=username,
        display_name=display_name[:128],
        date=int(message.date.timestamp()),
        text=text,
        reply_to_message_id=reply_to,
        message_thread_id=message.message_thread_id,
        edit_date=int(message.edit_date.timestamp()) if message.edit_date else None,
    )


class BotApp:
    """Обработчики Telegram. Единственные действия бота: сбор сообщений,
    выдача состояния и формирование сводок."""

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
        self.started_at = int(clock())
        self._tasks: set[asyncio.Task] = set()
        self._last_notice: dict[tuple[int, str], float] = {}

    # ---------------------------------------------------------------- утилиты

    def is_allowed_chat(self, chat) -> bool:
        return chat is not None and chat.type in GROUP_TYPES and chat.id in self.settings.group_chat_ids

    def is_allowed_user(self, user) -> bool:
        if user is None:
            return False
        return not self.settings.allowed_user_ids or user.id in self.settings.allowed_user_ids

    def _throttled(self, chat_id: int, key: str) -> bool:
        """True, если такое же уведомление недавно отправлялось (защита от флуда ответами)."""
        now = self.clock()
        last = self._last_notice.get((chat_id, key))
        if last is not None and now - last < NOTICE_THROTTLE_SECONDS:
            return True
        self._last_notice[(chat_id, key)] = now
        return False

    def _fmt(self, ts: int | float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(self.settings.tz).strftime("%d.%m.%Y %H:%M")

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_background(self, timeout: float) -> None:
        if not self._tasks:
            return
        logger.info("Ожидание завершения %s фоновых задач (до %s с)", len(self._tasks), timeout)
        done, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    async def _reply(message, text: str):
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

    # ------------------------------------------------------------- сообщения

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message or update.edited_message
        if message is None or not self.is_allowed_chat(message.chat):
            return
        record = extract_record(message, ignore_bots=self.settings.ignore_bot_messages)
        if record is None:
            return
        self.db.upsert_message(record, int(self.clock()), is_edit=update.edited_message is not None)

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None and message.migrate_to_chat_id:
            logger.warning(
                "Группа %s преобразована в супергруппу с ID %s — добавьте новый ID в TELEGRAM_GROUP_CHAT_IDS",
                message.chat.id, message.migrate_to_chat_id,
            )

    # --------------------------------------------------------------- команды

    async def cmd_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None:
            return
        if not self.is_allowed_chat(chat):
            logger.info("Отказ /summary: чат %s не разрешён", chat.id)
            if chat.type in GROUP_TYPES and not self._throttled(chat.id, "denied"):
                await self._reply(message, "Эта группа не подключена к боту сводок.")
            elif chat.type == "private":
                await self._reply(message, "Команда работает только в подключённых группах.")
            return
        if not self.is_allowed_user(user):
            logger.info("Отказ /summary: пользователь %s не в ALLOWED_USER_IDS", getattr(user, "id", None))
            if not self._throttled(chat.id, f"user-denied-{getattr(user, 'id', 0)}"):
                await self._reply(message, "У вас нет прав на запуск сводки в этой группе.")
            return

        period = parse_period(list(context.args or []))
        if period is None:
            await self._reply(message, "Формат: /summary, /summary 24h или /summary 7d")
            return

        claim = self.service.begin_manual(chat.id, user.id)
        if claim.status == "busy":
            if not self._throttled(chat.id, "busy"):
                await self._reply(message, "⏳ Сводка уже формируется, дождитесь результата.")
            return
        if claim.status == "cooldown":
            if not self._throttled(chat.id, "cooldown"):
                minutes = max(1, -(-(claim.retry_at - int(self.clock())) // 60))
                await self._reply(
                    message,
                    f"Ручную сводку можно запрашивать не чаще одного раза в "
                    f"{self.settings.manual_cooldown_seconds // 60} мин. на группу. "
                    f"Следующая будет доступна в {esc(self._fmt(claim.retry_at))} (через {minutes} мин.).",
                )
            return

        thread_id = message.message_thread_id if getattr(message, "is_topic_message", False) else None
        request = ManualRequest(
            chat_id=chat.id, user_id=user.id, period=period, reply_to=message.message_id, thread_id=thread_id
        )
        progress = None
        try:
            progress = await self._reply(message, f"⏳ Формирую сводку за {period.label}…")
        except Exception as exc:  # уведомление не критично; генерацию всё равно запускаем
            logger.warning("Не удалось отправить уведомление о начале генерации: %s", exc)
        self.spawn(self._manual_task(request, progress))

    async def _manual_task(self, request: ManualRequest, progress) -> None:
        try:
            await self.service.run_manual(request)
        finally:
            if progress is not None:
                try:
                    await progress.delete()
                except Exception:
                    pass

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        if not self.is_allowed_chat(chat):
            if chat.type in GROUP_TYPES and not self._throttled(chat.id, "denied"):
                await self._reply(message, "Эта группа не подключена к боту сводок.")
            return
        user = update.effective_user
        if not self.is_allowed_user(user):
            logger.info("Отказ /status: пользователь %s не в ALLOWED_USER_IDS", getattr(user, "id", None))
            if not self._throttled(chat.id, f"user-denied-{getattr(user, 'id', 0)}"):
                await self._reply(message, "У вас нет прав на просмотр состояния бота в этой группе.")
            return
        await self._reply(message, self.status_text(chat.id))

    def status_text(self, chat_id: int) -> str:
        s = self.settings
        now = int(self.clock())
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        lines = [
            "<b>🤖 Состояние сервиса</b>",
            f"Версия {APP_VERSION}, работает с {self._fmt(self.started_at)} ({esc(s.timezone)})",
            f"Модель: <code>{esc(s.llm_model)}</code>",
            f"Сообщений в базе: за 24 ч — {self.db.count_messages(chat_id, now - DAY)}, "
            f"всего — {self.db.count_messages(chat_id)} (хранение {s.message_retention_days} дн.)",
        ]
        if s.daily_enabled:
            nxt = next_daily_slot(now_dt, s.tz, s.daily_time)
            lines.append(f"Ежедневная сводка: в {s.daily_time:%H:%M}, следующая — {nxt:%d.%m.%Y %H:%M}")
        else:
            lines.append("Ежедневная сводка: выключена")
        if s.weekly_enabled:
            nxt = next_weekly_slot(now_dt, s.tz, s.weekly_day, s.weekly_time)
            lines.append(
                f"Еженедельная сводка: {WEEKDAY_NAMES_RU[s.weekly_day]} в {s.weekly_time:%H:%M}, "
                f"следующая — {nxt:%d.%m.%Y %H:%M}"
            )
        else:
            lines.append("Еженедельная сводка: выключена")

        state = self.db.get_manual_state(chat_id)
        if state and state["in_progress_until"] and state["in_progress_until"] > now:
            manual = "формируется"
        elif state and state["last_success_at"] and now < state["last_success_at"] + s.manual_cooldown_seconds:
            manual = f"доступна с {self._fmt(state['last_success_at'] + s.manual_cooldown_seconds)}"
        else:
            manual = "доступна"
        lines.append(f"Ручная сводка (/summary): {manual}")

        last = self.db.last_summary(chat_id)
        if last:
            details = (
                f"{KIND_RU.get(last.kind, last.kind)}, {self._fmt(last.created_at)} — "
                f"{STATUS_RU.get(last.status, last.status)}; сообщений: {last.message_count}"
            )
            if last.llm_calls:
                details += f"; вызовов LLM: {last.llm_calls}; токены: {last.prompt_tokens}/{last.completion_tokens}"
            if last.cost_usd is not None:
                details += f"; стоимость: ${last.cost_usd:.4f}"
            lines.append(f"Последняя генерация: {esc(details)}")
            if last.status == "failed" and last.error:
                lines.append(f"Ошибка: {esc(last.error[:200])}")
        else:
            lines.append("Последняя генерация: ещё не было")

        spent = self.db.llm_cost_since(now - DAY)
        budget = f" из ${s.llm_daily_budget_usd:.2f}" if s.llm_daily_budget_usd > 0 else ""
        lines.append(f"Расход OpenRouter за 24 ч: ${spent:.4f}{budget} ({self.db.llm_calls_since(now - DAY)} вызовов)")
        return "\n".join(lines)

    async def cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        if chat.type in GROUP_TYPES and not self.is_allowed_chat(chat) and self._throttled(chat.id, "chatid"):
            return
        text = f"ID этого чата: <code>{chat.id}</code>"
        user = update.effective_user
        if message.sender_chat is not None or (user is not None and user.id == ANONYMOUS_ADMIN_ID):
            text += "\nВы пишете от имени группы или канала — ваш личный ID скрыт. Отправьте /chatid от своего имени."
        elif user is not None:
            text += f"\nВаш user_id: <code>{user.id}</code>"
            if self.settings.allowed_user_ids:
                allowed = user.id in self.settings.allowed_user_ids
                text += " — " + ("есть в ALLOWED_USER_IDS" if allowed else "нет в ALLOWED_USER_IDS")
        if chat.type in GROUP_TYPES and not self.is_allowed_chat(chat):
            text += "\nГруппа не подключена: добавьте ID в TELEGRAM_GROUP_CHAT_IDS и перезапустите бота."
        await self._reply(message, text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        if chat.type in GROUP_TYPES and not self.is_allowed_chat(chat) and self._throttled(chat.id, "help"):
            return
        await self._reply(message, self.help_text())

    def help_text(self) -> str:
        s = self.settings
        schedule = []
        if s.daily_enabled:
            schedule.append(f"• каждый день в {s.daily_time:%H:%M} — сводка за последние 24 часа")
        if s.weekly_enabled:
            schedule.append(
                f"• {WEEKDAYS_ACCUSATIVE_RU[s.weekly_day]} в {s.weekly_time:%H:%M} — сводка за последние 7 дней"
            )
        schedule_text = "\n".join(schedule) if schedule else "• автоматические сводки выключены"
        who = ("Запрашивать могут только пользователи, назначенные администратором бота" if s.allowed_user_ids
               else "Запрашивать может любой участник группы")
        cooldown = s.manual_cooldown_seconds // 60
        return (
            "<b>📋 Бот сводок обсуждений</b>\n"
            "Читает переписку группы и публикует краткие сводки: основные темы, принятые решения "
            "и незакрытые вопросы — со ссылками на исходные сообщения.\n\n"
            f"<b>Автоматически</b> ({esc(s.timezone)}):\n{schedule_text}\n\n"
            "<b>По запросу</b>:\n"
            "/summary — сводка за последние 24 часа\n"
            "/summary 7d — сводка за последние 7 дней\n"
            f"{who}; не чаще одного раза в {cooldown} мин. на всю группу.\n\n"
            "<b>Прочее</b>:\n"
            "/status — состояние бота\n"
            "/chatid — ID группы и ваш user_id\n"
            "/help — эта справка\n\n"
            "Сводки составляет нейросеть, возможны неточности — сверяйтесь по ссылкам. "
            "Бот видит только текст и подписи к медиа; сообщения хранятся "
            f"{s.message_retention_days} дн. Просьбы и команды внутри переписки бот не выполняет."
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Ошибка обработки обновления: %s", context.error, exc_info=context.error)

    def register(self, application: Application) -> None:
        only_new = filters.UpdateType.MESSAGE  # правка команды не должна запускать её повторно
        application.add_handler(CommandHandler(["summary", "daily_summary"], self.cmd_summary, filters=only_new))
        application.add_handler(CommandHandler("status", self.cmd_status, filters=only_new))
        application.add_handler(CommandHandler("chatid", self.cmd_chatid, filters=only_new))
        application.add_handler(CommandHandler(["help", "start"], self.cmd_help, filters=only_new))
        application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, self.on_migrate))
        application.add_handler(
            MessageHandler(
                filters.ChatType.GROUPS
                & (filters.TEXT | filters.CAPTION)
                & ~filters.COMMAND
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
                self.on_message,
            )
        )
        application.add_error_handler(self.on_error)


BOT_COMMANDS = [
    BotCommand("summary", "Сводка за 24 часа (или /summary 7d)"),
    BotCommand("status", "Состояние сервиса"),
    BotCommand("chatid", "ID текущей группы"),
    BotCommand("help", "Список команд"),
]


async def _heartbeat(path: str, application: Application, tasks: list[asyncio.Task], stop: asyncio.Event) -> None:
    while not stop.is_set():
        healthy = application.updater is not None and application.updater.running and all(
            not t.done() for t in tasks
        )
        if healthy:
            try:
                Path(path).write_text(str(int(time.time())))
            except OSError as exc:
                logger.warning("Не удалось обновить heartbeat: %s", exc)
        else:
            logger.error("Heartbeat не обновлён: polling или фоновые задачи остановлены")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def run(settings: Settings) -> None:
    db = Database(settings.database_path)
    db.initialize(int(time.time()))
    llm = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_output_tokens,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    application = Application.builder().token(settings.telegram_bot_token).build()
    publisher = TelegramPublisher(application.bot)
    service = SummaryService(settings, db, SummaryGenerator(settings, llm), publisher)
    scheduler = Scheduler(settings, db, service)
    maintenance = Maintenance(settings, db)
    bot_app = BotApp(settings, db, service)
    bot_app.register(application)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with application:
            await application.start()
            await application.updater.start_polling(
                allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE],
                drop_pending_updates=False,
                bootstrap_retries=-1,
            )
            try:
                await application.bot.set_my_commands(BOT_COMMANDS)
            except Exception as exc:
                logger.warning("Не удалось установить список команд: %s", exc)
            me = application.bot.username
            logger.info(
                "Бот @%s v%s запущен; группы: %s; модель: %s",
                me, APP_VERSION, ", ".join(map(str, sorted(settings.group_chat_ids))), settings.llm_model,
            )
            if settings.allowed_user_ids:
                logger.info("/summary и /status разрешены пользователям: %s",
                            ", ".join(map(str, sorted(settings.allowed_user_ids))))
            else:
                logger.warning(
                    "ALLOWED_USER_IDS пуст: /summary и /status доступны всем участникам подключённых групп. "
                    "Чтобы ограничить, перечислите user_id через запятую (узнать свой — /chatid)"
                )
            workers = [
                asyncio.create_task(scheduler.run_forever(stop), name="scheduler"),
                asyncio.create_task(maintenance.run_forever(stop), name="maintenance"),
            ]
            heartbeat = asyncio.create_task(_heartbeat(settings.heartbeat_path, application, workers, stop))

            await stop.wait()
            logger.info("Получен сигнал остановки, завершение работы…")
            await application.updater.stop()
            await bot_app.wait_background(timeout=20)
            for task in (*workers, heartbeat):
                task.cancel()
            await asyncio.gather(*workers, heartbeat, return_exceptions=True)
            await application.stop()
    finally:
        await llm.aclose()
        db.close()
        try:
            os.unlink(settings.heartbeat_path)
        except OSError:
            pass
        logger.info("Бот остановлен")


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        setup_logging("INFO")
        for error in exc.errors:
            logger.error("Ошибка конфигурации: %s", error)
        logger.error("Заполните .env (см. .env.example) и перезапустите бота")
        sys.exit(2)
    setup_logging(settings.log_level, settings.secrets())
    try:
        asyncio.run(run(settings))
    except InvalidToken:
        logger.error("Telegram отклонил TELEGRAM_BOT_TOKEN: проверьте токен, выданный @BotFather")
        sys.exit(3)


if __name__ == "__main__":
    main()
