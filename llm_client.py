"""Минимальный клиент OpenAI-совместимого API OpenRouter (POST /chat/completions).

Используется httpx напрямую, чтобы полностью контролировать повторные попытки,
заголовок Retry-After, ошибки внутри HTTP 200 и учёт usage.cost. Ни ключ API,
ни содержимое запросов и ответов в журнал не пишутся.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    usage: LLMUsage
    model: str | None
    finish_reason: str | None
    latency_ms: int


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_message(body: object) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
    return ""


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
        max_retries: int,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "summary-bot",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_max * 5)
        delay = min(self._backoff_max, self._backoff_base * (2 ** attempt))
        return delay * random.uniform(0.5, 1.0)

    async def complete(self, messages: list[dict], *, json_mode: bool = True) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                error = LLMError(f"тайм-аут запроса ({type(exc).__name__})", retryable=True)
            except httpx.TransportError as exc:
                error = LLMError(f"сетевая ошибка ({type(exc).__name__})", retryable=True)
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                try:
                    return self._parse(response, latency_ms)
                except LLMError as exc:
                    error = exc

            if not error.retryable or attempt >= self.max_retries:
                logger.error(
                    "OpenRouter: запрос не выполнен (попытка %s/%s, HTTP %s): %s",
                    attempt + 1, self.max_retries + 1, error.status, error,
                )
                raise error
            delay = self._backoff(attempt, error.retry_after)
            logger.warning(
                "OpenRouter: временная ошибка (попытка %s/%s, HTTP %s): %s; повтор через %.1f с",
                attempt + 1, self.max_retries + 1, error.status, error, delay,
            )
            await self._sleep(delay)
            attempt += 1

    def _parse(self, response: httpx.Response, latency_ms: int) -> LLMResponse:
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = None

        if status != 200:
            message = _error_message(body) or f"HTTP {status}"
            retryable = status in RETRYABLE_STATUS or status >= 500
            raise LLMError(message, retryable=retryable, status=status, retry_after=retry_after)

        if not isinstance(body, dict):
            raise LLMError("ответ не является JSON-объектом", retryable=True, status=status)

        # Ошибки генерации могут приходить с HTTP 200
        if body.get("error"):
            err = body["error"]
            code = err.get("code") if isinstance(err, dict) else None
            code = code if isinstance(code, int) else None
            retryable = code is None or code in RETRYABLE_STATUS or code >= 500
            raise LLMError(_error_message(body) or "ошибка генерации", retryable=retryable, status=code)

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("в ответе нет choices", retryable=True, status=status)
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if finish_reason == "length":
            raise LLMError(
                "ответ модели обрезан по лимиту токенов (увеличьте LLM_MAX_OUTPUT_TOKENS)",
                retryable=False, status=status,
            )
        if finish_reason == "error":
            raise LLMError("модель завершила генерацию с ошибкой", retryable=True, status=status)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("пустой ответ модели", retryable=True, status=status)

        usage_raw = body.get("usage") if isinstance(body.get("usage"), dict) else {}

        def _num(key, cast):
            value = usage_raw.get(key)
            try:
                return cast(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        usage = LLMUsage(
            prompt_tokens=_num("prompt_tokens", int),
            completion_tokens=_num("completion_tokens", int),
            total_tokens=_num("total_tokens", int),
            cost_usd=_num("cost", float),
        )
        logger.info(
            "OpenRouter: ответ получен за %s мс, модель=%s, токены %s/%s, стоимость=%s",
            latency_ms, body.get("model"), usage.prompt_tokens, usage.completion_tokens, usage.cost_usd,
        )
        return LLMResponse(
            content=content, usage=usage, model=body.get("model"),
            finish_reason=finish_reason, latency_ms=latency_ms,
        )
