"""Генерация сводки переписки через LLM (OpenRouter).

Конвейер:
  1. Сообщения сериализуются в JSON-строки (по одной на сообщение) — так текст
     участников остаётся данными и не может «выйти» за границы разметки запроса.
  2. Общий объём ограничивается LLM_MAX_INPUT_CHARS (при превышении берутся самые
     новые сообщения), затем режется на фрагменты по LLM_CHUNK_MAX_CHARS.
  3. Один фрагмент — один запрос. Несколько — map-reduce: частичные сводки по
     фрагментам с message_ids, затем (при необходимости иерархическое) слияние.
  4. Ответ модели проверяется по схеме (pydantic); идентификаторы сообщений
     сверяются с реально переданной выборкой, несуществующие отбрасываются.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from config import PROMPTS_DIR, Settings
from database import MessageRecord
from llm_client import LLMError, LLMResponse, OpenRouterClient

logger = logging.getLogger(__name__)

MAX_IDS_PER_ITEM = 5
WEEKDAYS_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
LOG_BEGIN, LOG_END = "<<<CHAT_LOG_BEGIN>>>", "<<<CHAT_LOG_END>>>"
PARTIAL_BEGIN, PARTIAL_END = "<<<PARTIAL_SUMMARIES_BEGIN>>>", "<<<PARTIAL_SUMMARIES_END>>>"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


# ----------------------------------------------------------------- схема ответа

def _clip(limit: int) -> Callable[[Any], Any]:
    def validator(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, str):
            value = " ".join(value.split())
            return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
        return value

    return validator


def _ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            result.append(item)
        elif isinstance(item, str) and item.strip().lstrip("#").isdigit():
            result.append(int(item.strip().lstrip("#")))
    return result[:20]


def _limit_list(limit: int) -> Callable[[Any], Any]:
    def validator(value: Any) -> Any:
        if value is None:
            return []
        return value[:limit] if isinstance(value, list) else value

    return validator


IdList = Annotated[list[int], BeforeValidator(_ids)]


class Topic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: Annotated[str, BeforeValidator(_clip(150)), Field(min_length=1)]
    summary: Annotated[str, BeforeValidator(_clip(1500))] = ""
    message_ids: IdList = Field(default_factory=list)


class Point(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: Annotated[str, BeforeValidator(_clip(800)), Field(min_length=1)]
    message_ids: IdList = Field(default_factory=list)


class SummaryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topics: Annotated[list[Topic], BeforeValidator(_limit_list(15))]
    decisions: Annotated[list[Point], BeforeValidator(_limit_list(15))] = Field(default_factory=list)
    open_questions: Annotated[list[Point], BeforeValidator(_limit_list(15))] = Field(default_factory=list)
    overall: Annotated[str, BeforeValidator(_clip(1500))] = ""


class InvalidLLMOutput(ValueError):
    pass


def parse_llm_json(content: str) -> SummaryPayload:
    """Извлекает и валидирует JSON из ответа модели."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise InvalidLLMOutput("ответ не содержит JSON-объекта") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidLLMOutput(f"некорректный JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise InvalidLLMOutput("JSON не является объектом")
    try:
        return SummaryPayload.model_validate(data)
    except ValidationError as exc:
        raise InvalidLLMOutput(f"ответ не соответствует схеме ({exc.error_count()} ошибок)") from None


def sanitize_ids(payload: SummaryPayload, allowed: set[int]) -> tuple[SummaryPayload, int]:
    """Оставляет только идентификаторы реально существующих сообщений выборки."""
    dropped = 0

    def clean(ids: list[int]) -> list[int]:
        nonlocal dropped
        result: list[int] = []
        for mid in ids:
            if mid in allowed and mid not in result:
                result.append(mid)
            elif mid not in allowed:
                dropped += 1
        return result[:MAX_IDS_PER_ITEM]

    cleaned = SummaryPayload(
        topics=[Topic(title=t.title, summary=t.summary, message_ids=clean(t.message_ids)) for t in payload.topics],
        decisions=[Point(text=p.text, message_ids=clean(p.message_ids)) for p in payload.decisions],
        open_questions=[Point(text=p.text, message_ids=clean(p.message_ids)) for p in payload.open_questions],
        overall=payload.overall,
    )
    return cleaned, dropped


# ------------------------------------------------------------------ генерация

class SummaryGenerationError(Exception):
    """Сводку сформировать не удалось (после всех повторов)."""


@dataclass
class GenerationResult:
    payload: SummaryPayload
    message_count: int
    used_messages: int
    participants: int
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    dropped_ids: int = 0
    truncated: bool = False
    model: str | None = None


@dataclass
class _CallStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    model: str | None = None
    responses: list[LLMResponse] = field(default_factory=list)


UsageCallback = Callable[[LLMResponse], None]


def _clean_text(text: str, limit: int) -> str:
    text = _CONTROL_CHARS.sub("", text).replace("<<<", "‹‹‹").replace(">>>", "›››")
    if len(text) > limit:
        text = text[:limit].rstrip() + " …[обрезано]"
    return text


class SummaryGenerator:
    def __init__(self, settings: Settings, llm: OpenRouterClient, prompts_dir: Path = PROMPTS_DIR):
        self.settings = settings
        self.llm = llm
        self.tz = settings.tz
        self.system_prompt = (prompts_dir / "summary_system.txt").read_text(encoding="utf-8")
        self.merge_prompt = (prompts_dir / "summary_merge.txt").read_text(encoding="utf-8")

    # -- подготовка входных данных

    def _fmt_time(self, ts: int, with_year: bool = False) -> str:
        """Время с днём недели: без него модель неверно вычисляет «завтра», «в субботу» и т. п."""
        dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(self.tz)
        weekday = WEEKDAYS_SHORT_RU[dt.weekday()]
        return f"{weekday} " + dt.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")

    def message_line(self, m: MessageRecord) -> str:
        author = _clean_text(m.display_name or "Без имени", 64)
        if m.username:
            author = f"{author} (@{_clean_text(m.username, 40)})"
        return json.dumps(
            {
                "id": m.message_id,
                "time": self._fmt_time(m.date),
                "author": author,
                "reply_to": m.reply_to_message_id,
                "text": _clean_text(m.text, self.settings.max_message_chars),
            },
            ensure_ascii=False,
        )

    def _chunk(self, lines: list[str]) -> list[list[str]]:
        limit = self.settings.llm_chunk_max_chars
        chunks: list[list[str]] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if current and size + len(line) + 1 > limit:
                chunks.append(current)
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append(current)
        return chunks

    # -- вызовы LLM

    async def _call(
        self, system: str, user: str, stats: _CallStats, on_usage: UsageCallback | None
    ) -> SummaryPayload:
        attempts = 1 + self.settings.llm_invalid_json_retries
        last_error: Exception | None = None
        for attempt in range(attempts):
            if stats.calls >= self.settings.llm_max_calls_per_summary:
                raise SummaryGenerationError(
                    "превышен лимит обращений к LLM на одну сводку (LLM_MAX_CALLS_PER_SUMMARY)"
                )
            stats.calls += 1
            try:
                response = await self.llm.complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}]
                )
            except LLMError as exc:
                raise SummaryGenerationError(f"ошибка OpenRouter: {exc}") from exc
            stats.responses.append(response)
            stats.prompt_tokens += response.usage.prompt_tokens or 0
            stats.completion_tokens += response.usage.completion_tokens or 0
            if response.usage.cost_usd is not None:
                stats.cost_usd = (stats.cost_usd or 0.0) + response.usage.cost_usd
            stats.model = response.model or stats.model
            if on_usage:
                on_usage(response)
            try:
                return parse_llm_json(response.content)
            except InvalidLLMOutput as exc:
                last_error = exc
                logger.warning(
                    "Некорректный ответ модели (попытка %s/%s): %s", attempt + 1, attempts, exc
                )
        raise SummaryGenerationError(f"модель вернула некорректный результат: {last_error}")

    def _user_prompt(self, chunk: list[str], start: int, end: int, part: tuple[int, int] | None) -> str:
        header = [
            f"Период: {self._fmt_time(start, True)} — {self._fmt_time(end, True)} ({self.settings.timezone}).",
            f"Сообщений во фрагменте: {len(chunk)}.",
        ]
        if part:
            header.append(
                f"Это фрагмент {part[0]} из {part[1]} одной переписки (в хронологическом порядке). "
                "Составь сводку только по этому фрагменту; темы могут продолжаться в соседних фрагментах."
            )
        return "\n".join(header) + f"\n{LOG_BEGIN}\n" + "\n".join(chunk) + f"\n{LOG_END}"

    async def _reduce(
        self, partials: list[SummaryPayload], stats: _CallStats, on_usage: UsageCallback | None
    ) -> SummaryPayload:
        limit = self.settings.llm_chunk_max_chars
        serialized = [json.dumps(p.model_dump(), ensure_ascii=False) for p in partials]
        total = sum(len(s) + 2 for s in serialized)

        async def merge_directly() -> SummaryPayload:
            user = (
                f"Частичных сводок: {len(partials)}.\n{PARTIAL_BEGIN}\n["
                + ",\n".join(serialized)
                + f"]\n{PARTIAL_END}"
            )
            return await self._call(self.merge_prompt, user, stats, on_usage)

        if total <= limit or len(partials) <= 2:
            return await merge_directly()

        # Иерархическое слияние: группы, укладывающиеся в лимит (минимум по две сводки)
        groups: list[list[SummaryPayload]] = []
        current: list[SummaryPayload] = []
        size = 0
        for partial, text in zip(partials, serialized, strict=True):
            if len(current) >= 2 and size + len(text) + 2 > limit:
                groups.append(current)
                current, size = [], 0
            current.append(partial)
            size += len(text) + 2
        if current:
            if len(current) == 1 and groups:
                groups[-1].append(current[0])
            else:
                groups.append(current)
        if len(groups) == 1:
            return await merge_directly()
        merged =[await self._reduce(group, stats, on_usage) if len(group) > 1 else group[0] for group in groups]
        return await self._reduce(merged, stats, on_usage)

    async def generate(
        self,
        messages: list[MessageRecord],
        window_start: int,
        window_end: int,
        on_usage: UsageCallback | None = None,
    ) -> GenerationResult:
        if not messages:
            raise SummaryGenerationError("нет сообщений для сводки")

        lines = [self.message_line(m) for m in messages]
        # Ограничение объёма: берём самые новые сообщения, укладывающиеся в лимит
        budget = self.settings.llm_max_input_chars
        keep_from = len(lines)
        used = 0
        while keep_from > 0 and used + len(lines[keep_from - 1]) + 1 <= budget:
            keep_from -= 1
            used += len(lines[keep_from]) + 1
        keep_from = min(keep_from, len(lines) - 1)  # хотя бы одно сообщение
        truncated = keep_from > 0

        chunks = self._chunk(lines[keep_from:])
        max_map = max(1, self.settings.llm_max_calls_per_summary - 1) if len(chunks) > 1 else 1
        if len(chunks) > max_map:
            dropped_chunks = len(chunks) - max_map
            keep_from += sum(len(c) for c in chunks[:dropped_chunks])
            chunks = chunks[dropped_chunks:]
            truncated = True
        used_messages = messages[keep_from:]
        allowed = {m.message_id for m in used_messages}
        if truncated:
            logger.warning(
                "Объём переписки превышает лимиты: в сводку вошли последние %s из %s сообщений",
                len(used_messages), len(messages),
            )

        stats = _CallStats()
        if len(chunks) == 1:
            payload = await self._call(
                self.system_prompt, self._user_prompt(chunks[0], window_start, window_end, None), stats, on_usage
            )
        else:
            partials = []
            for index, chunk in enumerate(chunks, start=1):
                partial = await self._call(
                    self.system_prompt,
                    self._user_prompt(chunk, window_start, window_end, (index, len(chunks))),
                    stats,
                    on_usage,
                )
                partial, _ = sanitize_ids(partial, allowed)
                partials.append(partial)
            payload = await self._reduce(partials, stats, on_usage)

        payload, dropped = sanitize_ids(payload, allowed)
        if dropped:
            logger.warning("Отброшено %s несуществующих идентификаторов сообщений из ответа модели", dropped)

        participants = len({m.user_id if m.user_id is not None else m.display_name for m in messages})
        return GenerationResult(
            payload=payload,
            message_count=len(messages),
            used_messages=len(used_messages),
            participants=participants,
            llm_calls=stats.calls,
            prompt_tokens=stats.prompt_tokens,
            completion_tokens=stats.completion_tokens,
            cost_usd=stats.cost_usd,
            dropped_ids=dropped,
            truncated=truncated,
            model=stats.model,
        )
