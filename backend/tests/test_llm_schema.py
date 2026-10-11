from __future__ import annotations

import json

from app.llm.prompts.loader import load_prompt, render_prompt
from app.llm.schema import response_format_for, schema_hint, strict_json_schema
from app.llm.usage import UsageRecord, UsageTracker
from app.models.llm_io import ProductDraft


def test_every_object_forbids_extra_properties() -> None:
    schema = strict_json_schema(ProductDraft)
    objects = _objects(schema)
    assert objects
    for node in objects:
        assert node["additionalProperties"] is False


def test_every_property_is_required() -> None:
    for node in _objects(strict_json_schema(ProductDraft)):
        assert sorted(node["required"]) == sorted(node["properties"])


def test_nested_models_are_strictified_too() -> None:
    schema = strict_json_schema(ProductDraft)
    assert "$defs" in schema
    nested = [d for d in schema["$defs"].values() if isinstance(d.get("properties"), dict)]
    assert nested
    for definition in nested:
        assert definition["additionalProperties"] is False
    enums = [d for d in schema["$defs"].values() if "enum" in d]
    assert enums
    for definition in enums:
        assert "additionalProperties" not in definition


def test_defaults_are_stripped() -> None:
    assert "default" not in json.dumps(strict_json_schema(ProductDraft))


def test_response_format_is_strict_and_named() -> None:
    payload = response_format_for(ProductDraft)
    assert payload["type"] == "json_schema"
    assert payload["json_schema"]["name"] == "ProductDraft"
    assert payload["json_schema"]["strict"] is True


def test_schema_hint_is_stable_json() -> None:
    assert json.loads(schema_hint(ProductDraft)) == strict_json_schema(ProductDraft)


def test_prompts_are_loaded_and_cached() -> None:
    first = load_prompt("normalize_product")
    assert "Never invent a fact" in first
    assert load_prompt("normalize_product") is first


def test_prompt_placeholders_are_substituted() -> None:
    rendered = render_prompt(
        "normalize_product", source_platform="csv", source_hint="", record="{}"
    )
    assert "$source_platform" not in rendered
    assert "came from a csv source" in rendered


def test_unknown_placeholders_do_not_crash_rendering() -> None:
    assert render_prompt("repair_json", errors="boom") is not None


def test_usage_totals_aggregate_by_stage_and_model() -> None:
    tracker = UsageTracker()
    tracker.record(
        UsageRecord(
            stage="normalize",
            model="nemotron",
            structured_mode="json_schema",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            latency_ms=900,
        )
    )
    tracker.record(
        UsageRecord(
            stage="map",
            model="nemotron",
            structured_mode="json_schema",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_ms=100,
            repairs=1,
            failed=True,
        )
    )
    totals = tracker.totals()
    assert totals.calls == 2
    assert totals.failures == 1
    assert totals.total_tokens == 165
    assert totals.latency_ms == 1000
    assert totals.repairs == 1
    assert totals.tokens_by_stage == {"normalize": 150, "map": 15}
    assert totals.tokens_by_model == {"nemotron": 165}


def _objects(schema: dict[str, object]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found
