"""Telegram-бот дневного саммари группового чата.

Логирует сообщения из групп, указанных в TELEGRAM_GROUP_CHAT_IDS,
и каждый день в SUMMARY_TIME присылает в чат дайджест обсуждений
со ссылками на начало каждой темы.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from config import (
    SUMMARY_TIME,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_GROUP_CHAT_IDS,
    TIMEZONE,
    parse_summary_time,
    validate_config,
)
from summary_generator import SummaryGenerator

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def is_allowed_chat(chat_id: int | str) -> bool:
    return str(chat_id) in TELEGRAM_GROUP_CHAT_IDS


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохраняет текстовые сообщения (и подписи к медиа) из разрешённых групп"""
    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user or user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if not is_allowed_chat(chat.id):
        logger.debug(f"Чат {chat.id} не в списке разрешенных чатов")
        return

    content = message.text or message.caption or ""
    if not content:
        return  # Если нет ни текста, ни caption, пропускаем

    user_name = user.first_name or ""
    if user.last_name:
        user_name = f"{user_name} {user.last_name}".strip()
    if not user_name:
        user_name = user.username or "Unknown"

    reply_to_id = None
    if message.reply_to_message:
        reply_to_id = str(message.reply_to_message.message_id)

    try:
        database.save_message(
            chat_id=str(chat.id),
            user_id=str(user.id),
            user_name=user_name,
            message_id=str(message.message_id),
            content=content,
            reply_to_message_id=reply_to_id,
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения в daily_logs: {e}")


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручная генерация саммари командой /summary (без удаления сообщений из базы)"""
    message = update.message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not is_allowed_chat(chat.id):
        await message.reply_text("Этот чат не настроен для саммари (TELEGRAM_GROUP_CHAT_IDS).")
        return

    status = await message.reply_text("Генерирую саммари за последние 24 часа...")
    generator = SummaryGenerator()
    summary = await generator.generate_daily_summary(
        chat_id=str(chat.id), chat_title=chat.title, cleanup=False
    )
    if summary:
        await status.delete()
        await message.reply_text(
            summary, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )
    else:
        await status.edit_text("Нет сообщений за последние 24 часа — саммари пока не из чего собирать.")


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /chatid — показывает ID чата (удобно для заполнения .env)"""
    chat = update.effective_chat
    if update.message and chat:
        await update.message.reply_text(f"ID этого чата: `{chat.id}`", parse_mode=ParseMode.MARKDOWN)


async def send_summaries(application: Application) -> None:
    """Генерирует и отправляет саммари во все настроенные чаты"""
    generator = SummaryGenerator()
    for chat_id in TELEGRAM_GROUP_CHAT_IDS:
        try:
            try:
                chat_info = await application.bot.get_chat(chat_id)
                chat_title = chat_info.title
            except Exception:
                chat_title = "Unknown Chat"

            summary = await generator.generate_daily_summary(chat_id=chat_id, chat_title=chat_title)
            if summary:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=summary,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                logger.info(f"Саммари отправлено в чат {chat_id}")
            else:
                logger.info(f"Нет данных для саммари в чате {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки саммари в чат {chat_id}: {e}")


async def daily_summary_scheduler(application: Application) -> None:
    """Ежедневно в SUMMARY_TIME (в таймзоне TIMEZONE) запускает рассылку саммари"""
    tz = ZoneInfo(TIMEZONE)
    hour, minute = parse_summary_time()

    while True:
        try:
            now = datetime.now(tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(
                f"Следующая отправка саммари: {target.strftime('%d.%m.%Y %H:%M %Z')} "
                f"(через {wait_seconds / 3600:.1f} ч)"
            )
            await asyncio.sleep(wait_seconds)

            logger.info(f"Запуск автоматической генерации саммари ({SUMMARY_TIME})")
            await send_summaries(application)

            # Страховочная очистка совсем старых записей (если генерация не сработала)
            deleted = database.cleanup_old_logs(database.utcnow() - timedelta(hours=48))
            if deleted:
                logger.info(f"Очищено {deleted} устаревших записей из daily_logs (старше 48ч)")
        except Exception as e:
            logger.error(f"Ошибка в задаче daily_summary_scheduler: {e}")
            await asyncio.sleep(60)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


async def main() -> None:
    errors = validate_config()
    if errors:
        for err in errors:
            logger.error(f"Ошибка конфигурации: {err}")
        raise SystemExit("Заполните .env (см. .env.example) и перезапустите бота")

    database.init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("daily_summary", summary_command))
    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_group_message,
        )
    )
    application.add_error_handler(error_handler)

    logger.info("Starting Telegram bot...")
    scheduler_task = None
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        scheduler_task = asyncio.create_task(daily_summary_scheduler(application))
        logger.info(
            f"Задача отправки ежедневного саммари запущена ({SUMMARY_TIME} {TIMEZONE}), "
            f"чаты: {', '.join(TELEGRAM_GROUP_CHAT_IDS)}"
        )

        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bot stopped by user")
    finally:
        if scheduler_task:
            scheduler_task.cancel()
        if application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
