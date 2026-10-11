from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import LLMTransportError, SchemaValidationError
from app.llm.prompts.loader import render_prompt
from app.llm.schema import response_format_for, schema_hint
from app.llm.usage import UsageRecord, UsageTracker
from app.logging import get_logger, log_event

T = TypeVar("T", bound=BaseModel)
CreateFn = Callable[..., Awaitable[Any]]

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS = (APIConnectionError, APITimeoutError, TimeoutError, ConnectionError)
BAD_REQUEST = 400
FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

logger = get_logger(__name__)


class StructuredMode(StrEnum):
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    TEXT = "text"


MODE_FALLBACK: dict[StructuredMode, StructuredMode] = {
    StructuredMode.JSON_SCHEMA: StructuredMode.JSON_OBJECT,
    StructuredMode.JSON_OBJECT: StructuredMode.TEXT,
}


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        *,
        create: CreateFn | None = None,
        tracker: UsageTracker | None = None,
    ) -> None:
        self.settings = settings
        self.tracker = tracker or UsageTracker()
        self._create = create if create is not None else self._build_create()
        self._modes: dict[str, StructuredMode] = {}

    def _build_create(self) -> CreateFn:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.settings.require_api_key(),
            base_url=self.settings.nebius_base_url,
            timeout=self.settings.http_timeout,
        )
        return client.chat.completions.create

    async def complete_json(
        self,
        *,
        prompt: str,
        response_model: type[T],
        stage: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
    ) -> tuple[T, UsageRecord]:
        target = model or self.settings.llm_model
        temp = self.settings.llm_temperature if temperature is None else temperature
        started = time.monotonic()
        tokens = [0, 0, 0]
        attempts_used = 0
        repairs = 0
        follow_up: list[dict[str, str]] = []

        while True:
            content, usage, attempts, mode = await self._call(
                prompt, system, follow_up, target, temp, response_model
            )
            attempts_used += attempts
            self._accumulate(tokens, usage)
            try:
                instance = response_model.model_validate(_extract_json(content))
            except (ValidationError, ValueError) as exc:
                if repairs >= self.settings.llm_max_repairs:
                    self._write_record(
                        stage, target, mode, tokens, started, attempts_used, repairs, failed=True
                    )
                    raise SchemaValidationError(response_model.__name__, str(exc), content) from exc
                repairs += 1
                log_event(logger, "repairing invalid model output", stage=stage, model=target)
                follow_up = [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": render_prompt(
                            "repair_json", errors=str(exc), schema=schema_hint(response_model)
                        ),
                    },
                ]
                continue
            record = self._write_record(
                stage, target, mode, tokens, started, attempts_used, repairs, failed=False
            )
            return instance, record

    async def _call(
        self,
        prompt: str,
        system: str | None,
        follow_up: list[dict[str, str]],
        model: str,
        temperature: float,
        response_model: type[BaseModel],
    ) -> tuple[str, object, int, StructuredMode]:
        mode = self._modes.get(model, StructuredMode.JSON_SCHEMA)
        attempts = 0
        last_error: Exception | None = None
        while attempts < self.settings.llm_max_attempts:
            attempts += 1
            try:
                response = await self._create(
                    **self._payload(
                        prompt, system, follow_up, model, temperature, response_model, mode
                    )
                )
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                await self._backoff(attempts)
                continue
            except Exception as exc:
                status = _status_of(exc)
                if status == BAD_REQUEST and mode in MODE_FALLBACK:
                    mode = MODE_FALLBACK[mode]
                    log_event(
                        logger, "falling back to a looser output mode", model=model, mode=mode
                    )
                    continue
                if status in RETRYABLE_STATUS:
                    last_error = exc
                    await self._backoff(attempts)
                    continue
                raise LLMTransportError(f"{type(exc).__name__}: {exc}") from exc
            self._modes[model] = mode
            return _content_of(response), getattr(response, "usage", None), attempts, mode
        raise LLMTransportError(f"gave up after {attempts} attempts: {last_error}")

    def _payload(
        self,
        prompt: str,
        system: str | None,
        follow_up: list[dict[str, str]],
        model: str,
        temperature: float,
        response_model: type[BaseModel],
        mode: StructuredMode,
    ) -> dict[str, Any]:
        user_content = prompt
        if mode is not StructuredMode.JSON_SCHEMA:
            user_content = (
                f"{prompt}\n\nReturn JSON only, matching this schema exactly:\n\n"
                f"{schema_hint(response_model)}"
            )
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        messages.extend(follow_up)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if mode is StructuredMode.JSON_SCHEMA:
            payload["response_format"] = response_format_for(response_model)
        elif mode is StructuredMode.JSON_OBJECT:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _backoff(self, attempt: int) -> None:
        delay = self.settings.connector_backoff_base * (2 ** (attempt - 1))
        await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))

    def _accumulate(self, tokens: list[int], usage: object) -> None:
        if usage is None:
            return
        tokens[0] += int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens[1] += int(getattr(usage, "completion_tokens", 0) or 0)
        tokens[2] += int(getattr(usage, "total_tokens", 0) or 0)

    def _write_record(
        self,
        stage: str,
        model: str,
        mode: StructuredMode,
        tokens: list[int],
        started: float,
        attempts: int,
        repairs: int,
        *,
        failed: bool,
    ) -> UsageRecord:
        return self.tracker.record(
            UsageRecord(
                stage=stage,
                model=model,
                structured_mode=mode.value,
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                total_tokens=tokens[2] or tokens[0] + tokens[1],
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
                repairs=repairs,
                failed=failed,
            )
        )


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _content_of(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise LLMTransportError("model returned no choices")
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if not isinstance(content, str) or not content.strip():
        raise LLMTransportError("model returned empty content")
    return content


def _extract_json(content: str) -> object:
    text = content.strip()
    fenced = FENCED_JSON.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])
