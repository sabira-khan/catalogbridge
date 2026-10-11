from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.errors import LLMTransportError, MissingAPIKeyError, SchemaValidationError
from app.llm.client import LLMClient, StructuredMode
from app.models.llm_io import ProductDraft

from tests.conftest import SettingsFactory

VALID_DRAFT = {
    "title": "Merino Runner",
    "description": "Wool shoe",
    "brand": "Allbirds",
    "attributes": [{"name": "Material", "value": "merino wool", "unit": None, "origin": "source"}],
    "variants": [{"sku": "MR-42", "title": "42", "options": [], "price": "129.00"}],
    "tags": ["shoes"],
    "confidence": 0.9,
    "notes": None,
}


@dataclass
class FakeUsage:
    prompt_tokens: int = 120
    completion_tokens: int = 40
    total_tokens: int = 160


@dataclass
class FakeMessage:
    content: str | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"status {status_code}")


def reply(payload: object) -> FakeResponse:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return FakeResponse(choices=[FakeChoice(FakeMessage(body))])


class Recorder:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def client(settings_factory: SettingsFactory, recorder: Recorder) -> LLMClient:
    settings = settings_factory(nebius_api_key="test-key")
    settings.connector_backoff_base = 0.0
    return LLMClient(settings, create=recorder)


async def test_valid_response_is_parsed_into_the_model(
    settings_factory: SettingsFactory,
) -> None:
    recorder = Recorder(reply(VALID_DRAFT))
    draft, usage = await client(settings_factory, recorder).complete_json(
        prompt="normalize this", response_model=ProductDraft, stage="normalize"
    )
    assert isinstance(draft, ProductDraft)
    assert draft.title == "Merino Runner"
    assert draft.confidence == 0.9
    assert usage.stage == "normalize"
    assert usage.total_tokens == 160
    assert usage.repairs == 0
    assert usage.failed is False


async def test_strict_schema_is_sent_on_the_first_attempt(
    settings_factory: SettingsFactory,
) -> None:
    recorder = Recorder(reply(VALID_DRAFT))
    await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    response_format = recorder.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True


async def test_bad_request_falls_back_to_json_object_mode(
    settings_factory: SettingsFactory,
) -> None:
    recorder = Recorder(FakeStatusError(400), reply(VALID_DRAFT))
    _, usage = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert usage.structured_mode == StructuredMode.JSON_OBJECT.value
    assert recorder.calls[1]["response_format"] == {"type": "json_object"}
    assert "matching this schema exactly" in recorder.calls[1]["messages"][-1]["content"]


async def test_second_bad_request_falls_back_to_plain_text(
    settings_factory: SettingsFactory,
) -> None:
    recorder = Recorder(FakeStatusError(400), FakeStatusError(400), reply(VALID_DRAFT))
    _, usage = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert usage.structured_mode == StructuredMode.TEXT.value
    assert "response_format" not in recorder.calls[2]


async def test_working_mode_is_remembered_per_model(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(FakeStatusError(400), reply(VALID_DRAFT), reply(VALID_DRAFT))
    instance = client(settings_factory, recorder)
    await instance.complete_json(prompt="p", response_model=ProductDraft, stage="normalize")
    await instance.complete_json(prompt="p", response_model=ProductDraft, stage="normalize")
    assert recorder.calls[2]["response_format"] == {"type": "json_object"}


async def test_rate_limit_is_retried(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(FakeStatusError(429), reply(VALID_DRAFT))
    _, usage = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert usage.attempts == 2


async def test_transport_error_is_retried(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(TimeoutError("slow"), reply(VALID_DRAFT))
    _, usage = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert usage.attempts == 2


async def test_retries_are_exhausted_and_reported(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(*[FakeStatusError(503)] * 6)
    with pytest.raises(LLMTransportError):
        await client(settings_factory, recorder).complete_json(
            prompt="p", response_model=ProductDraft, stage="normalize"
        )


async def test_unexpected_status_is_not_retried(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(FakeStatusError(403))
    with pytest.raises(LLMTransportError):
        await client(settings_factory, recorder).complete_json(
            prompt="p", response_model=ProductDraft, stage="normalize"
        )
    assert len(recorder.calls) == 1


async def test_invalid_output_triggers_one_repair_round_trip(
    settings_factory: SettingsFactory,
) -> None:
    broken = {**VALID_DRAFT, "confidence": 5.0}
    recorder = Recorder(reply(broken), reply(VALID_DRAFT))
    draft, usage = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert draft.confidence == 0.9
    assert usage.repairs == 1
    assert usage.total_tokens == 320
    repair_messages = recorder.calls[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert "did not validate" in repair_messages[-1]["content"]


async def test_exhausted_repairs_raise_and_still_record_usage(
    settings_factory: SettingsFactory,
) -> None:
    broken = reply({**VALID_DRAFT, "confidence": 5.0})
    recorder = Recorder(broken, broken)
    instance = client(settings_factory, recorder)
    with pytest.raises(SchemaValidationError) as excinfo:
        await instance.complete_json(
            prompt="p", response_model=ProductDraft, stage="normalize"
        )
    assert excinfo.value.model_name == "ProductDraft"
    assert instance.tracker.totals().failures == 1
    assert instance.tracker.totals().total_tokens == 320


async def test_fenced_json_is_extracted(settings_factory: SettingsFactory) -> None:
    fenced = f"Here you go:\n```json\n{json.dumps(VALID_DRAFT)}\n```\nHope that helps."
    recorder = Recorder(reply(fenced))
    draft, _ = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert draft.title == "Merino Runner"


async def test_json_surrounded_by_prose_is_extracted(settings_factory: SettingsFactory) -> None:
    noisy = f"Sure. {json.dumps(VALID_DRAFT)} Let me know if you need changes."
    recorder = Recorder(reply(noisy))
    draft, _ = await client(settings_factory, recorder).complete_json(
        prompt="p", response_model=ProductDraft, stage="normalize"
    )
    assert draft.brand == "Allbirds"


async def test_empty_content_is_a_transport_error(settings_factory: SettingsFactory) -> None:
    recorder = Recorder(FakeResponse(choices=[FakeChoice(FakeMessage(None))]))
    with pytest.raises(LLMTransportError):
        await client(settings_factory, recorder).complete_json(
            prompt="p", response_model=ProductDraft, stage="normalize"
        )


async def test_stage_and_model_are_attributed_in_usage(
    settings_factory: SettingsFactory,
) -> None:
    recorder = Recorder(reply(VALID_DRAFT))
    instance = client(settings_factory, recorder)
    await instance.complete_json(
        prompt="p",
        response_model=ProductDraft,
        stage="normalize",
        model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    )
    totals = instance.tracker.totals()
    assert totals.tokens_by_stage == {"normalize": 160}
    assert totals.tokens_by_model == {"nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": 160}
    assert recorder.calls[0]["model"] == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def test_building_a_client_without_a_key_fails_loudly(
    settings_factory: SettingsFactory,
) -> None:
    with pytest.raises(MissingAPIKeyError):
        LLMClient(settings_factory())
