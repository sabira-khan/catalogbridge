from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from app.logging import get_logger, log_event

logger = get_logger(__name__)


class UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    stage: str
    model: str
    structured_mode: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    repairs: int = 0
    failed: bool = False


class UsageTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    repairs: int = 0
    tokens_by_stage: dict[str, int] = Field(default_factory=dict)
    tokens_by_model: dict[str, int] = Field(default_factory=dict)


class UsageTracker:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, record: UsageRecord) -> UsageRecord:
        self.records.append(record)
        log_event(logger, "llm call", **record.model_dump())
        return record

    def totals(self) -> UsageTotals:
        by_stage: dict[str, int] = defaultdict(int)
        by_model: dict[str, int] = defaultdict(int)
        totals = UsageTotals(calls=len(self.records))
        for record in self.records:
            totals.failures += int(record.failed)
            totals.prompt_tokens += record.prompt_tokens
            totals.completion_tokens += record.completion_tokens
            totals.total_tokens += record.total_tokens
            totals.latency_ms += record.latency_ms
            totals.repairs += record.repairs
            by_stage[record.stage] += record.total_tokens
            by_model[record.model] += record.total_tokens
        totals.tokens_by_stage = dict(by_stage)
        totals.tokens_by_model = dict(by_model)
        return totals
