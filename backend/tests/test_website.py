from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from app.connectors.base import SourceContext
from app.connectors.jsonld import extract_jsonld_blocks, find_products
from app.connectors.registry import build_client
from app.connectors.website import WebsiteConnector
from app.errors import NormalizationError
from app.llm.client import LLMClient
from app.models.enums import AttributeOrigin, SourcePlatform
from app.pipeline.website import WebsiteNormalizer, normalize_jsonld_product

from tests.conftest import SettingsFactory

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SITE = "https://examplesite.com"
FETCHED_AT = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)
ALLOW_ALL = "User-agent: *\nAllow: /\n"


def html(name: str) -> str:
    return (FIXTURES / name).read_text()


def jsonld_product() -> dict[str, Any]:
    products = find_products(extract_jsonld_blocks(html("product_jsonld.html")))
    return products[0]


def context() -> SourceContext:
    return SourceContext(
        platform=SourcePlatform.WEBSITE,
        base_url=SITE,
        store_name="Northfield",
        fetched_at=FETCHED_AT,
    )


def connector(
    settings_factory: SettingsFactory, client: httpx.AsyncClient, source: str = SITE
) -> WebsiteConnector:
    settings = settings_factory()
    settings.connector_page_delay = 0.0
    settings.connector_backoff_base = 0.0
    return WebsiteConnector(source, settings, client)


async def collect(instance: WebsiteConnector) -> list[dict[str, Any]]:
    return [record async for record in instance.iter_records()]


def test_jsonld_blocks_are_extracted_and_non_products_ignored() -> None:
    blocks = extract_jsonld_blocks(html("product_jsonld.html"))
    assert len(blocks) == 2
    products = find_products(blocks)
    assert len(products) == 3
    assert products[0]["name"] == "Merino Crew Jumper"


def test_pages_without_jsonld_yield_nothing() -> None:
    assert find_products(extract_jsonld_blocks(html("product_no_jsonld.html"))) == []


def test_malformed_jsonld_is_skipped() -> None:
    assert extract_jsonld_blocks('<script type="application/ld+json">{not json}</script>') == []


def test_plain_scripts_are_not_treated_as_jsonld() -> None:
    assert extract_jsonld_blocks('<script>var a = {"b": 1};</script>') == []


def test_graph_wrapped_products_are_found() -> None:
    payload = {"@graph": [{"@type": "Organization"}, {"@type": "Product", "name": "Alpha"}]}
    assert [p["name"] for p in find_products(payload)] == ["Alpha"]


def test_aggregate_offer_gives_price_and_compare_price() -> None:
    product = normalize_jsonld_product(jsonld_product(), context())
    assert product.price == Decimal("89.00")
    assert product.compare_at_price == Decimal("120.00")
    assert product.currency == "GBP"


def test_brand_object_is_flattened() -> None:
    assert normalize_jsonld_product(jsonld_product(), context()).brand == "Northfield"


def test_description_html_is_stripped() -> None:
    product = normalize_jsonld_product(jsonld_product(), context())
    assert product.description == "A fine gauge merino jumper."


def test_duplicate_and_protocol_relative_images_are_handled() -> None:
    product = normalize_jsonld_product(jsonld_product(), context())
    assert [image.url for image in product.images] == [
        "https://cdn.example.com/jumper-1.jpg",
        "https://cdn.example.com/jumper-2.jpg",
    ]


def test_has_variant_becomes_canonical_variants() -> None:
    product = normalize_jsonld_product(jsonld_product(), context())
    assert len(product.variants) == 2
    assert [v.sku for v in product.variants] == ["MCJ-001-S", "MCJ-001-M"]
    assert [o.value for o in product.variants[0].options] == ["S", "Navy"]
    assert product.variants[0].available is True
    assert product.variants[1].available is False


def test_additional_property_becomes_an_attribute_with_a_unit() -> None:
    product = normalize_jsonld_product(jsonld_product(), context())
    weight = next(a for a in product.attributes if a.name == "Net weight")
    assert (weight.value, weight.unit) == ("340", "g")
    assert weight.origin is AttributeOrigin.SOURCE


def test_extra_schema_fields_become_attributes() -> None:
    names = {a.name for a in normalize_jsonld_product(jsonld_product(), context()).attributes}
    assert "Material" in names
    assert "Color" in names


def test_schema_plumbing_keys_are_not_attributes() -> None:
    names = {a.name for a in normalize_jsonld_product(jsonld_product(), context()).attributes}
    assert not {"@type", "@context", "Sku", "Offers", "Hasvariant"} & names


def test_sku_becomes_the_source_id() -> None:
    assert normalize_jsonld_product(jsonld_product(), context()).source_id == "MCJ-001"


def test_jsonld_product_without_a_name_is_rejected() -> None:
    with pytest.raises(NormalizationError, match="no name"):
        normalize_jsonld_product({"@type": "Product", "sku": "X"}, context())


@respx.mock
async def test_sitemap_product_urls_are_crawled(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(f"{SITE}/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "sitemap.xml").read_text(), headers={"content-type": "text/xml"}
        )
    )
    page = httpx.Response(
        200, text=html("product_jsonld.html"), headers={"content-type": "text/html"}
    )
    respx.get(f"{SITE}/product/merino-crew-jumper").mock(return_value=page)
    respx.get(f"{SITE}/product/selvedge-denim-jacket").mock(return_value=page)

    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert len(records) == 3
    assert all(r["name"] for r in records)


@respx.mock
async def test_non_product_sitemap_urls_are_skipped(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(f"{SITE}/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "sitemap.xml").read_text(), headers={"content-type": "text/xml"}
        )
    )
    about = respx.get(f"{SITE}/pages/about")
    page = httpx.Response(
        200, text=html("product_jsonld.html"), headers={"content-type": "text/html"}
    )
    respx.get(f"{SITE}/product/merino-crew-jumper").mock(return_value=page)
    respx.get(f"{SITE}/product/selvedge-denim-jacket").mock(return_value=page)

    async with build_client(settings_factory()) as client:
        await collect(connector(settings_factory, client))
    assert about.call_count == 0


@respx.mock
async def test_robots_disallow_blocks_a_product_path(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /product/\n")
    )
    respx.get(f"{SITE}/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "sitemap.xml").read_text(), headers={"content-type": "text/xml"}
        )
    )
    blocked = respx.get(f"{SITE}/product/merino-crew-jumper")

    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert records == []
    assert blocked.call_count == 0


@respx.mock
async def test_missing_robots_is_treated_as_allowed(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get(SITE).mock(
        return_value=httpx.Response(
            200, text=html("product_jsonld.html"), headers={"content-type": "text/html"}
        )
    )
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert len(records) == 3


@respx.mock
async def test_a_product_url_entry_point_fetches_only_that_page(
    settings_factory: SettingsFactory,
) -> None:
    url = f"{SITE}/product/merino-crew-jumper"
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    sitemap = respx.get(f"{SITE}/sitemap.xml")
    respx.get(url).mock(
        return_value=httpx.Response(
            200, text=html("product_jsonld.html"), headers={"content-type": "text/html"}
        )
    )
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client, url))
    assert len(records) == 3
    assert sitemap.call_count == 0


@respx.mock
async def test_a_page_without_jsonld_is_queued_for_llm_extraction(
    settings_factory: SettingsFactory,
) -> None:
    url = f"{SITE}/product/selvedge-denim-jacket"
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(url).mock(
        return_value=httpx.Response(
            200, text=html("product_no_jsonld.html"), headers={"content-type": "text/html"}
        )
    )
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client, url))
    assert len(records) == 1
    assert records[0]["_extraction"] == "html"
    assert "Selvedge Denim Jacket" in records[0]["title_hint"]


@respx.mock
async def test_discover_reads_the_page_title(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(SITE).mock(
        return_value=httpx.Response(
            200, text=html("product_jsonld.html"), headers={"content-type": "text/html"}
        )
    )
    async with build_client(settings_factory()) as client:
        found = await connector(settings_factory, client).discover()
    assert found.store_name == "Merino Crew Jumper | Northfield"
    assert found.platform is SourcePlatform.WEBSITE


@dataclass
class FakeUsage:
    prompt_tokens: int = 500
    completion_tokens: int = 100
    total_tokens: int = 600


@dataclass
class FakeMessage:
    content: str


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage = field(default_factory=FakeUsage)


class Replies:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return FakeResponse(choices=[FakeChoice(FakeMessage(json.dumps(self.payload)))])


async def test_html_fallback_goes_through_the_model(settings_factory: SettingsFactory) -> None:
    draft = {
        "title": "Selvedge Denim Jacket",
        "description": "14oz unwashed selvedge denim.",
        "brand": "Northfield",
        "attributes": [
            {"name": "Weight", "value": "980", "unit": "g", "origin": "llm"}
        ],
        "variants": [{"sku": None, "title": "L", "options": [], "price": "189.00"}],
        "tags": ["denim"],
        "confidence": 0.72,
        "notes": "Price read from the visible price element",
    }
    replies = Replies(draft)
    settings = settings_factory(nebius_api_key="k")
    normalizer = WebsiteNormalizer(LLMClient(settings, create=replies))
    record = {
        "_extraction": "html",
        "url": f"{SITE}/product/selvedge-denim-jacket",
        "title_hint": "Selvedge Denim Jacket | Northfield",
        "html": html("product_no_jsonld.html"),
    }
    product = await normalizer.normalize(record, context())
    assert product.title == "Selvedge Denim Jacket"
    assert product.price == Decimal("189.00")
    assert product.attributes[0].origin is AttributeOrigin.LLM
    assert product.raw["confidence"] == 0.72
    assert "Page URL:" in replies.calls[0]["messages"][-1]["content"]


async def test_jsonld_records_never_reach_the_model(settings_factory: SettingsFactory) -> None:
    replies = Replies({})
    settings = settings_factory(nebius_api_key="k")
    normalizer = WebsiteNormalizer(LLMClient(settings, create=replies))
    await normalizer.normalize(jsonld_product(), context())
    assert replies.calls == []


async def test_html_fallback_without_an_llm_client_fails_clearly() -> None:
    normalizer = WebsiteNormalizer()
    with pytest.raises(NormalizationError, match="needs an llm client"):
        await normalizer.normalize(
            {"_extraction": "html", "url": SITE, "html": "<html/>"}, context()
        )
