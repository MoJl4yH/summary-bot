"""Бизнес-логика сводок: генерация за окно времени, ручные запросы с ограничением
частоты, плановые задачи с повторами и идемпотентной публикацией."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from config import Settings
from database import Database, ManualClaim, ScheduledRun
from formatting import esc, format_period, plural, render_summary, split_blocks
from llm_client import LLMResponse
from summary_generator import SummaryGenerationError, SummaryGenerator
from telegram_io import PublishError, TelegramPublisher

logger = logging.getLogger(__name__)

DAY = 86400
WEEK = 7 * DAY
MANUAL_LEASE_SECONDS = 30 * 60
KIND_RU = {"daily": "ежедневная", "weekly": "еженедельная", "manual": "ручная"}


@dataclass(frozen=True)
class Period:
    seconds: int
    label: str      # «24 часа»
    heading: str    # «Сводка за 24 часа»


PERIOD_24H = Period(DAY, "24 часа", "Сводка за 24 часа")
PERIOD_7D = Period(WEEK, "7 дней", "Сводка за 7 дней")


def parse_period(args: list[str]) -> Period | None:
    if not args:
        return PERIOD_24H
    value = args[0].strip().lower()
    if value in ("24h", "24ч", "1d", "1д", "day", "сутки", "день", "24"):
        return PERIOD_24H
    if value in ("7d", "7д", "1w", "week", "неделя", "168h", "7"):
        return PERIOD_7D
    return None


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class BuildOutcome:
    summary_id: int
    status: str  # "ready" | "empty"
    parts: list[str]
    message_count: int


@dataclass(frozen=True)
class ManualRequest:
    chat_id: int
    user_id: int | None
    period: Period
    reply_to: int | None = None
    thread_id: int | None = None


class SummaryService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        generator: SummaryGenerator,
        publisher: TelegramPublisher,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.db = db
        self.generator = generator
        self.publisher = publisher
        self.clock = clock

    def now(self) -> int:
        return int(self.clock())

    # ------------------------------------------------------------ генерация

    def check_budget(self) -> None:
        budget = self.settings.llm_daily_budget_usd
        if budget > 0:
            spent = self.db.llm_cost_since(self.now() - DAY)
            if spent >= budget:
                raise BudgetExceeded(
                    f"достигнут лимит расходов на OpenRouter за сутки (${spent:.4f} из ${budget:.2f})"
                )

    async def build_summary(
        self,
        chat_id: int,
        kind: str,
        window_start: int,
        window_end: int,
        heading: str,
        *,
        period_key: str | None = None,
        requested_by: int | None = None,
        on_created: Callable[[int], None] | None = None,
    ) -> BuildOutcome:
        summary_id = self.db.create_summary(
            chat_id, kind, window_start, window_end, self.now(),
            period_key=period_key, requested_by=requested_by, model=self.settings.llm_model,
        )
        if on_created:
            on_created(summary_id)
        messages = self.db.get_messages(chat_id, window_start, window_end)
        if len(messages) < self.settings.min_messages:
            self.db.update_summary(summary_id, self.now(), status="empty", message_count=len(messages))
            logger.info(
                "Чат %s: %s сообщений за период — меньше порога %s, LLM не вызывается",
                chat_id, len(messages), self.settings.min_messages,
            )
            return BuildOutcome(summary_id, "empty", [], len(messages))

        try:
            self.check_budget()
        except BudgetExceeded as exc:
            self.db.update_summary(summary_id, self.now(), status="failed", error=str(exc), message_count=len(messages))
            raise

        def on_usage(response: LLMResponse) -> None:
            self.db.record_llm_usage(
                self.now(), chat_id=chat_id, summary_id=summary_id,
                model=response.model or self.settings.llm_model, status="ok",
                prompt_tokens=response.usage.prompt_tokens, completion_tokens=response.usage.completion_tokens,
                cost_usd=response.usage.cost_usd, latency_ms=response.latency_ms,
            )

        logger.info("Чат %s: генерация сводки (%s) по %s сообщениям", chat_id, kind, len(messages))
        try:
            result = await self.generator.generate(messages, window_start, window_end, on_usage)
        except SummaryGenerationError as exc:
            self.db.update_summary(
                summary_id, self.now(), status="failed", error=str(exc)[:500], message_count=len(messages)
            )
            raise

        blocks = render_summary(
            result.payload,
            chat_id=chat_id,
            heading=heading,
            period_text=f"{format_period(window_start, window_end, self.settings.tz)} ({self.settings.timezone})",
            message_count=result.message_count,
            participants=result.participants,
            used_messages=result.used_messages,
        )
        parts = split_blocks(blocks)
        self.db.update_summary(
            summary_id, self.now(), status="ready", parts=parts, message_count=result.message_count,
            llm_calls=result.llm_calls, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, cost_usd=result.cost_usd,
            model=result.model or self.settings.llm_model,
        )
        logger.info(
            "Чат %s: сводка #%s готова (%s частей, %s вызовов LLM, токены %s/%s, стоимость %s)",
            chat_id, summary_id, len(parts), result.llm_calls, result.prompt_tokens,
            result.completion_tokens, result.cost_usd,
        )
        return BuildOutcome(summary_id, "ready", parts, result.message_count)

    async def deliver(
        self,
        summary_id: int,
        chat_id: int,
        parts: list[str],
        *,
        start: int = 0,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        def on_sent(count: int) -> None:
            self.db.update_summary(summary_id, self.now(), parts_sent=count)

        try:
            await self.publisher.send_parts(
                chat_id, parts, start=start, reply_to=reply_to, thread_id=thread_id, on_sent=on_sent
            )
        except PublishError as exc:
            self.db.update_summary(summary_id, self.now(), error=f"ошибка отправки: {exc}"[:500])
            raise
        self.db.update_summary(summary_id, self.now(), status="sent", sent_at=self.now(), error=None)
        logger.info("Чат %s: сводка #%s опубликована", chat_id, summary_id)

    # ------------------------------------------------------- ручные сводки

    def begin_manual(self, chat_id: int, user_id: int | None) -> ManualClaim:
        return self.db.try_begin_manual(
            chat_id, user_id, self.now(), self.settings.manual_cooldown_seconds, MANUAL_LEASE_SECONDS
        )

    async def _notify(self, req: ManualRequest, text: str) -> None:
        try:
            await self.publisher.send(req.chat_id, esc(text), reply_to=req.reply_to, thread_id=req.thread_id)
        except PublishError as exc:
            logger.warning("Не удалось отправить уведомление в чат %s: %s", req.chat_id, exc)

    async def run_manual(self, req: ManualRequest) -> bool:
        """Выполняет ручную сводку после успешного begin_manual. Лимит расходуется
        только если сводка сформирована и полностью отправлена."""
        success = False
        try:
            end = self.now()
            start = end - req.period.seconds
            outcome = await self.build_summary(
                req.chat_id, "manual", start, end, req.period.heading, requested_by=req.user_id
            )
            if outcome.status == "empty":
                n = outcome.message_count
                await self._notify(
                    req,
                    f"За последние {req.period.label} {n} {plural(n, 'сообщение', 'сообщения', 'сообщений')} — "
                    f"недостаточно для сводки (минимум {self.settings.min_messages}). Лимит запросов не израсходован.",
                )
                return False
            await self.deliver(
                outcome.summary_id, req.chat_id, outcome.parts, reply_to=req.reply_to, thread_id=req.thread_id
            )
            success = True
            return True
        except BudgetExceeded as exc:
            logger.warning("Чат %s: ручная сводка отклонена: %s", req.chat_id, exc)
            await self._notify(req, "Достигнут суточный лимит расходов на генерацию сводок. Попробуйте позже.")
        except SummaryGenerationError as exc:
            logger.error("Чат %s: ручная сводка не сформирована: %s", req.chat_id, exc)
            await self._notify(
                req, "Не удалось сформировать сводку из-за ошибки сервиса генерации. "
                     "Лимит запросов не израсходован — попробуйте позже.",
            )
        except PublishError as exc:
            logger.error("Чат %s: ручная сводка не отправлена: %s", req.chat_id, exc)
        except Exception:
            logger.exception("Чат %s: непредвиденная ошибка ручной сводки", req.chat_id)
            await self._notify(req, "Внутренняя ошибка при формировании сводки. Попробуйте позже.")
        finally:
            self.db.finish_manual(req.chat_id, success=success, now=self.now())
        return False

    # ------------------------------------------------------- плановые сводки

    def _fail_run(self, run: ScheduledRun, error: str, *, retryable: bool) -> None:
        now = self.now()
        if retryable and run.attempts < self.settings.scheduled_max_attempts:
            delay = min(3600, 300 * 2 ** (run.attempts - 1))
            self.db.finish_scheduled_run(
                run.chat_id, run.kind, run.period_key, status="failed", now=now,
                error=error[:500], next_attempt_at=now + delay,
            )
            logger.error(
                "Плановая сводка %s/%s (%s) не выполнена, попытка %s: %s; повтор через %s с",
                run.chat_id, run.kind, run.period_key, run.attempts, error, delay,
            )
        else:
            self.db.finish_scheduled_run(
                run.chat_id, run.kind, run.period_key, status="gave_up", now=now, error=error[:500]
            )
            logger.error(
                "Плановая сводка %s/%s (%s) окончательно не выполнена: %s",
                run.chat_id, run.kind, run.period_key, error,
            )

    async def run_scheduled(self, run: ScheduledRun) -> None:
        period = PERIOD_24H if run.kind == "daily" else PERIOD_7D
        heading = "Ежедневная сводка за 24 часа" if run.kind == "daily" else "Еженедельная сводка за 7 дней"
        try:
            existing = self.db.get_summary(run.summary_id) if run.summary_id else None
            if existing and existing.status == "sent":
                self.db.finish_scheduled_run(run.chat_id, run.kind, run.period_key, status="done", now=self.now())
                return
            if existing and existing.status == "ready" and existing.parts:
                logger.info(
                    "Плановая сводка %s/%s (%s): продолжение отправки с части %s из %s без повторной генерации",
                    run.chat_id, run.kind, run.period_key, existing.parts_sent + 1, len(existing.parts),
                )
                summary_id, parts, start = existing.id, existing.parts, existing.parts_sent
            else:
                outcome = await self.build_summary(
                    run.chat_id, run.kind, run.window_start, run.window_end, heading,
                    period_key=run.period_key,
                    on_created=lambda sid: self.db.set_run_summary(
                        run.chat_id, run.kind, run.period_key, sid, self.now()
                    ),
                )
                if outcome.status == "empty":
                    if self.settings.post_empty_auto_summary:
                        n = outcome.message_count
                        await self.publisher.send(
                            run.chat_id,
                            esc(
                                f"📋 {heading}: {n} {plural(n, 'сообщение', 'сообщения', 'сообщений')} "
                                f"за {period.label} — недостаточно для сводки."
                            ),
                            thread_id=self.settings.summary_thread_id(run.chat_id),
                        )
                    self.db.finish_scheduled_run(
                        run.chat_id, run.kind, run.period_key, status="skipped", now=self.now()
                    )
                    return
                summary_id, parts, start = outcome.summary_id, outcome.parts, 0
            await self.deliver(
                summary_id, run.chat_id, parts, start=start, thread_id=self.settings.summary_thread_id(run.chat_id)
            )
            self.db.finish_scheduled_run(run.chat_id, run.kind, run.period_key, status="done", now=self.now())
        except (SummaryGenerationError, BudgetExceeded) as exc:
            self._fail_run(run, str(exc), retryable=True)
        except PublishError as exc:
            self._fail_run(run, str(exc), retryable=exc.retryable)
        except Exception as exc:
            logger.exception("Непредвиденная ошибка плановой сводки %s/%s", run.chat_id, run.kind)
            self._fail_run(run, f"{type(exc).__name__}: {exc}", retryable=True)
