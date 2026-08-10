"""Генерация дневного саммари обсуждений чата через LLM (OpenRouter)"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import openai

import database
from config import LLM_MODEL, OPENROUTER_API_KEY, PROMPT_PATH, TIMEZONE

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "Ты — редактор дайджеста. Твоя задача — прочитать лог чата "
    "и составить краткую выжимку (саммари) обсуждений за день."
)


def message_link(chat_id: str, message_id: str) -> str:
    """Ссылка на сообщение: https://t.me/c/CHAT_ID/MESSAGE_ID (ID супергруппы без префикса -100)"""
    clean_chat_id = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{clean_chat_id}/{message_id}"


class SummaryGenerator:
    def __init__(self):
        if not OPENROUTER_API_KEY:
            logger.error("OPENROUTER_API_KEY не найден в переменных окружения")

        timeout_config = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        self.llm_client = openai.AsyncOpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            http_client=httpx.AsyncClient(timeout=timeout_config),
        )
        self.tz = ZoneInfo(TIMEZONE)

    def _format_log(self, logs: list[database.DailyLog]) -> str:
        """Форматирует сообщения в текстовый лог для LLM, с контекстом реплаев"""
        messages_by_id = {log.message_id: log for log in logs}
        lines = []
        for log in logs:
            local_time = log.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.tz)
            time_str = local_time.strftime("%H:%M")

            if log.reply_to_message_id:
                lines.append(
                    f"[{time_str}] {log.user_name} (ID: {log.message_id}, "
                    f"реплай на ID: {log.reply_to_message_id}): {log.content}"
                )
                replied = messages_by_id.get(log.reply_to_message_id)
                if replied:
                    replied_time = replied.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(self.tz)
                    lines.append(
                        f"  ↳ [Исходное сообщение от {replied.user_name} "
                        f"({replied_time.strftime('%H:%M')}, ID: {replied.message_id})]: {replied.content}"
                    )
                else:
                    lines.append(
                        f"  ↳ [Исходное сообщение (ID: {log.reply_to_message_id}) не найдено в логах]"
                    )
            else:
                lines.append(f"[{time_str}] {log.user_name} (ID: {log.message_id}): {log.content}")
        return "\n".join(lines)

    def _load_prompt(self) -> str:
        try:
            return PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning(f"Промпт {PROMPT_PATH} не найден, используется fallback")
            return FALLBACK_PROMPT

    def _format_digest(self, chat_id: str, llm_output: str) -> str | None:
        """Парсит JSON от LLM и собирает итоговое сообщение-дайджест"""
        cleaned = llm_output.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]

        try:
            data = json.loads(cleaned.strip())
        except json.JSONDecodeError:
            logger.error(f"Ошибка парсинга JSON от LLM: {llm_output}")
            # Fallback: возвращаем сырой ответ, если это текст
            return llm_output

        topics = data.get("topics", [])
        if not topics:
            logger.info(f"LLM не выделила тем в чате {chat_id}")
            return None

        today_str = datetime.now(self.tz).strftime("%d.%m.%Y")
        lines = [f"📅 **Дайджест за {today_str}**", ""]

        for topic in topics:
            title = topic.get("title", "Без темы")
            desc = topic.get("summary", "")
            msg_id = topic.get("start_message_id")
            if msg_id:
                lines.append(f"🔹 [{title}]({message_link(chat_id, msg_id)}): {desc}")
            else:
                lines.append(f"🔹 **{title}**: {desc}")

        day_mood = data.get("day_mood")
        if day_mood:
            lines.append("")
            lines.append(f"**Итог дня:** {day_mood}")

        return "\n".join(lines)

    async def generate_daily_summary(
        self, chat_id: str, chat_title: str | None = None, cleanup: bool = True
    ) -> str | None:
        """
        Генерирует саммари за последние 24 часа для указанного чата.
        cleanup=True: удаляет обработанные сообщения (для расписания).
        cleanup=False: не удаляет сообщения (для ручного вызова).
        """
        try:
            since_time = database.utcnow() - timedelta(hours=24)
            logs = database.get_messages_since(str(chat_id), since_time)

            if not logs:
                logger.info(f"Нет сообщений для саммари в чате {chat_id}")
                return None

            logger.info(f"Найдено {len(logs)} сообщений для саммари в чате {chat_id}")
            messages_text = self._format_log(logs)

            response = await self.llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": self._load_prompt()},
                    {
                        "role": "user",
                        "content": f"Вот лог сообщений чата '{chat_title or chat_id}':\n\n{messages_text}",
                    },
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            llm_output = response.choices[0].message.content
            summary = self._format_digest(str(chat_id), llm_output)

            if cleanup and summary:
                deleted = database.delete_messages([log.id for log in logs])
                logger.info(f"Удалено {deleted} обработанных сообщений из daily_logs")
            elif not cleanup:
                logger.info("Cleanup=False, сообщения не удалены (ручной запуск)")

            return summary
        except Exception as e:
            logger.exception(f"Ошибка генерации саммари для чата {chat_id}: {e}")
            return None
