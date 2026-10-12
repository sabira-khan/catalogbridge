from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.config import Settings
from app.errors import LLMTransportError
from app.llm.client import RETRYABLE_EXCEPTIONS, RETRYABLE_STATUS, _status_of
from app.llm.usage import UsageRecord, UsageTracker
from app.logging import get_logger, log_event

EmbedFn = Callable[..., Awaitable[Any]]
DEFAULT_BATCH_SIZE = 64

logger = get_logger(__name__)


class EmbeddingClient:
    def __init__(
        self,
        settings: Settings,
        *,
        embed: EmbedFn | None = None,
        tracker: UsageTracker | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.settings = settings
        self.tracker = tracker or UsageTracker()
        self.batch_size = batch_size
        self._embed = embed if embed is not None else self._build_embed()
        self._cache: dict[str, list[float]] = {}

    def _build_embed(self) -> EmbedFn:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.settings.require_api_key(),
            base_url=self.settings.nebius_base_url,
            timeout=self.settings.http_timeout,
        )
        return client.embeddings.create

    async def embed_texts(self, texts: Sequence[str], *, stage: str) -> list[list[float]]:
        missing = [text for text in dict.fromkeys(texts) if text not in self._cache]
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            vectors, usage = await self._embed_batch(batch, stage)
            for text, vector in zip(batch, vectors, strict=True):
                self._cache[text] = normalize(vector)
            self.tracker.record(usage)
        return [self._cache[text] for text in texts]

    async def _embed_batch(
        self, batch: list[str], stage: str
    ) -> tuple[list[list[float]], UsageRecord]:
        attempts = 0
        last_error: Exception | None = None
        while attempts < self.settings.llm_max_attempts:
            attempts += 1
            try:
                response = await self._embed(model=self.settings.embed_model, input=batch)
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                await self._backoff(attempts)
                continue
            except Exception as exc:
                if _status_of(exc) in RETRYABLE_STATUS:
                    last_error = exc
                    await self._backoff(attempts)
                    continue
                raise LLMTransportError(f"{type(exc).__name__}: {exc}") from exc
            return _vectors_of(response, len(batch)), UsageRecord(
                stage=stage,
                model=self.settings.embed_model,
                structured_mode="embedding",
                prompt_tokens=_prompt_tokens(response),
                total_tokens=_prompt_tokens(response),
                attempts=attempts,
            )
        raise LLMTransportError(f"embeddings gave up after {attempts} attempts: {last_error}")

    async def _backoff(self, attempt: int) -> None:
        delay = self.settings.connector_backoff_base * (2 ** (attempt - 1))
        await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))


def normalize(vector: Sequence[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return [0.0] * len(vector)
    return [value / magnitude for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError(f"vector length mismatch: {len(left)} and {len(right)}")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _vectors_of(response: object, expected: int) -> list[list[float]]:
    data = getattr(response, "data", None)
    if not isinstance(data, list) or len(data) != expected:
        found = 0 if data is None else len(data)
        raise LLMTransportError(
            f"embedding response held {found} vectors, expected {expected}"
        )
    vectors: list[list[float]] = []
    for item in data:
        embedding = getattr(item, "embedding", None)
        if not isinstance(embedding, list) or not embedding:
            raise LLMTransportError("embedding response contained an empty vector")
        vectors.append([float(value) for value in embedding])
    return vectors


def _prompt_tokens(response: object) -> int:
    usage = getattr(response, "usage", None)
    tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    try:
        return int(tokens or 0)
    except (TypeError, ValueError):
        log_event(logger, "embedding usage was not a number")
        return 0
