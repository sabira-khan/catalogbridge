from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import respx
from app.cli import app
from app.models.canonical import Product
from typer.testing import CliRunner

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORE = "https://examplestore.com"
runner = CliRunner()


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def mock_store() -> None:
    respx.get(f"{STORE}/meta.json").mock(
        return_value=httpx.Response(200, json=fixture("shopify_meta.json"))
    )
    route = respx.get(f"{STORE}/products.json")
    route.side_effect = [
        httpx.Response(200, json=fixture("shopify_page_1.json")),
        httpx.Response(200, json=fixture("shopify_page_empty.json")),
    ]


@respx.mock
def test_extract_prints_a_table() -> None:
    mock_store()
    result = runner.invoke(app, ["extract", STORE, "--i-own-this-catalog"])
    assert result.exit_code == 0, result.output
    assert "Allbirds" in result.output
    assert "2 products" in result.output
    assert "currency USD" in result.output


@respx.mock
def test_extract_json_output_revalidates_as_canonical_products() -> None:
    mock_store()
    result = runner.invoke(app, ["extract", STORE, "--format", "json", "--i-own-this-catalog"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source"]["currency"] == "USD"
    assert len(payload["products"]) == 2
    for record in payload["products"]:
        assert Product.model_validate(record).source_id == record["source_id"]


@respx.mock
def test_extract_jsonl_output_is_one_product_per_line() -> None:
    mock_store()
    result = runner.invoke(app, ["extract", STORE, "--format", "jsonl", "--i-own-this-catalog"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        Product.model_validate(json.loads(line))


@respx.mock
def test_extract_never_leaks_the_raw_payload() -> None:
    mock_store()
    result = runner.invoke(app, ["extract", STORE, "--format", "json", "--i-own-this-catalog"])
    assert "body_html" not in result.output
    assert '"raw"' not in result.output


@respx.mock
def test_extract_honours_limit() -> None:
    mock_store()
    result = runner.invoke(
        app, ["extract", STORE, "--limit", "1", "--format", "jsonl", "--i-own-this-catalog"]
    )
    assert result.exit_code == 0, result.output
    assert len([line for line in result.output.splitlines() if line.strip()]) == 1


@respx.mock
def test_extract_writes_to_a_file(tmp_path: pathlib.Path) -> None:
    mock_store()
    destination = tmp_path / "products.json"
    result = runner.invoke(
        app,
        ["extract", STORE, "--format", "json", "--out", str(destination), "--i-own-this-catalog"],
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 2 products" in result.output
    assert len(json.loads(destination.read_text())["products"]) == 2


@respx.mock
def test_consent_is_required_before_any_request() -> None:
    route = respx.get(f"{STORE}/products.json")
    result = runner.invoke(app, ["extract", STORE], input="n\n")
    assert result.exit_code != 0
    assert "own or have authorization" in result.output
    assert route.call_count == 0


@respx.mock
def test_consent_can_be_given_interactively() -> None:
    mock_store()
    result = runner.invoke(app, ["extract", STORE], input="y\n")
    assert result.exit_code == 0, result.output
    assert "2 products" in result.output


@respx.mock
def test_non_shopify_store_exits_with_a_readable_message() -> None:
    respx.get(f"{STORE}/meta.json").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{STORE}/products.json").mock(
        return_value=httpx.Response(200, html="<html></html>")
    )
    result = runner.invoke(app, ["extract", STORE, "--i-own-this-catalog"])
    assert result.exit_code == 1
    assert "does not serve a Shopify products feed" in result.output


def test_unusable_url_exits_with_a_readable_message() -> None:
    result = runner.invoke(app, ["extract", "not a url", "--i-own-this-catalog"])
    assert result.exit_code == 1
    assert "not a usable store URL" in result.output


def test_llm_check_requires_a_key() -> None:
    result = runner.invoke(app, ["llm-check"])
    assert result.exit_code == 1
    assert "NEBIUS_API_KEY is not set" in result.output


def test_version_is_printed() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


@respx.mock
def test_map_requires_a_known_target() -> None:
    result = runner.invoke(app, ["map", STORE, "--target", "nope", "--i-own-this-catalog"])
    assert result.exit_code == 1
    assert "No target spec with id nope" in result.output


@respx.mock
def test_map_without_a_key_suggests_the_keyless_path() -> None:
    result = runner.invoke(
        app, ["map", STORE, "--target", "meridian_apparel", "--i-own-this-catalog"]
    )
    assert result.exit_code == 1
    assert "NEBIUS_API_KEY is not set" in result.output
    assert "--candidates-only --retriever keyword" in result.output


@respx.mock
def test_candidates_only_with_keyword_retriever_needs_no_key() -> None:
    mock_store()
    result = runner.invoke(
        app,
        [
            "map",
            STORE,
            "--target",
            "meridian_apparel",
            "--candidates-only",
            "--retriever",
            "keyword",
            "--i-own-this-catalog",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No model calls were made" in result.output


@respx.mock
def test_candidates_only_still_needs_a_key_for_embeddings() -> None:
    result = runner.invoke(
        app,
        [
            "map",
            STORE,
            "--target",
            "meridian_apparel",
            "--candidates-only",
            "--retriever",
            "embedding",
            "--i-own-this-catalog",
        ],
    )
    assert result.exit_code == 1
    assert "NEBIUS_API_KEY is not set" in result.output
