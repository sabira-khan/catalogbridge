from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest
import respx
from app.connectors.registry import build_client, get_connector
from app.connectors.shopify import ShopifyConnector, normalize_store_url
from app.errors import (
    ConnectorHTTPError,
    StoreNotShopifyError,
    StorePasswordProtectedError,
    UnsupportedSourceError,
)
from app.models.enums import SourcePlatform

from tests.conftest import SettingsFactory

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORE = "https://examplestore.com"
PRODUCTS_URL = f"{STORE}/products.json"
META_URL = f"{STORE}/meta.json"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def json_response(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


async def collect(connector: ShopifyConnector) -> list[dict[str, Any]]:
    return [record async for record in connector.iter_records()]


def make_connector(
    settings_factory: SettingsFactory, client: httpx.AsyncClient, source: str = STORE
) -> ShopifyConnector:
    settings = settings_factory()
    settings.connector_page_delay = 0.0
    settings.connector_backoff_base = 0.0
    return ShopifyConnector(source, settings, client)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("examplestore.com", "https://examplestore.com"),
        ("https://examplestore.com", "https://examplestore.com"),
        ("http://examplestore.com", "https://examplestore.com"),
        ("https://examplestore.com/", "https://examplestore.com"),
        ("https://examplestore.com/collections/all", "https://examplestore.com"),
        ("https://examplestore.com/products/some-shoe?variant=1", "https://examplestore.com"),
        ("  https://ExampleStore.com  ", "https://examplestore.com"),
        ("https://examplestore.com:443/products.json", "https://examplestore.com"),
        ("https://shop.examplestore.co.uk/pages/about", "https://shop.examplestore.co.uk"),
    ],
)
def test_store_url_is_normalized(raw: str, expected: str) -> None:
    assert normalize_store_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://examplestore.com", "localhost", "not a url"])
def test_unusable_store_urls_are_rejected(raw: str) -> None:
    with pytest.raises(UnsupportedSourceError):
        normalize_store_url(raw)


@respx.mock
async def test_pagination_walks_until_an_empty_page(settings_factory: SettingsFactory) -> None:
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [
        json_response(fixture("shopify_page_1.json")),
        json_response(fixture("shopify_page_2.json")),
        json_response(fixture("shopify_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    assert len(records) == 4
    assert route.call_count == 3
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2", "3"]


@respx.mock
async def test_short_page_does_not_stop_pagination(settings_factory: SettingsFactory) -> None:
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [
        json_response(fixture("shopify_page_1.json")),
        json_response(fixture("shopify_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    assert len(records) == 2
    assert route.call_count == 2


@respx.mock
async def test_store_that_ignores_the_page_parameter_stops_early(
    settings_factory: SettingsFactory,
) -> None:
    route = respx.get(PRODUCTS_URL).mock(
        return_value=json_response(fixture("shopify_page_1.json"))
    )
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    assert len(records) == 2
    assert route.call_count == 2


@respx.mock
async def test_max_pages_caps_the_walk(settings_factory: SettingsFactory) -> None:
    pages = [
        {"products": [{"id": i, "title": f"Product {i}"}]}
        for i in range(1, 12)
    ]
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [json_response(p) for p in pages]
    async with build_client(settings_factory()) as client:
        connector = make_connector(settings_factory, client)
        connector.settings.connector_max_pages = 3
        records = await collect(connector)
    assert len(records) == 3
    assert route.call_count == 3


@respx.mock
async def test_rate_limit_is_retried_then_succeeds(settings_factory: SettingsFactory) -> None:
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        json_response(fixture("shopify_page_1.json")),
        json_response(fixture("shopify_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    assert len(records) == 2
    assert route.call_count == 3


@respx.mock
async def test_persistent_rate_limit_raises(settings_factory: SettingsFactory) -> None:
    respx.get(PRODUCTS_URL).mock(return_value=httpx.Response(429))
    async with build_client(settings_factory()) as client:
        connector = make_connector(settings_factory, client)
        with pytest.raises(ConnectorHTTPError) as excinfo:
            await collect(connector)
    assert excinfo.value.status_code == 429


@respx.mock
async def test_transport_error_is_retried(settings_factory: SettingsFactory) -> None:
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [
        httpx.ConnectTimeout("timed out"),
        json_response(fixture("shopify_page_1.json")),
        json_response(fixture("shopify_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    assert len(records) == 2


@respx.mock
async def test_html_response_is_not_a_shopify_store(settings_factory: SettingsFactory) -> None:
    respx.get(PRODUCTS_URL).mock(
        return_value=httpx.Response(200, html="<html><body>Hello</body></html>")
    )
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotShopifyError):
            await collect(make_connector(settings_factory, client))


@respx.mock
async def test_missing_products_key_is_not_a_shopify_store(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(PRODUCTS_URL).mock(return_value=json_response({"errors": "Not found"}))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotShopifyError):
            await collect(make_connector(settings_factory, client))


@respx.mock
async def test_404_is_not_a_shopify_store(settings_factory: SettingsFactory) -> None:
    respx.get(PRODUCTS_URL).mock(return_value=httpx.Response(404, json={}))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotShopifyError):
            await collect(make_connector(settings_factory, client))


@respx.mock
async def test_password_protected_store_is_detected(settings_factory: SettingsFactory) -> None:
    respx.get(PRODUCTS_URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"{STORE}/password"})
    )
    respx.get(f"{STORE}/password").mock(return_value=httpx.Response(200, html="<html></html>"))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StorePasswordProtectedError):
            await collect(make_connector(settings_factory, client))


@respx.mock
async def test_discover_reads_currency_from_meta(settings_factory: SettingsFactory) -> None:
    respx.get(META_URL).mock(return_value=json_response(fixture("shopify_meta.json")))
    async with build_client(settings_factory()) as client:
        context = await make_connector(settings_factory, client).discover()
    assert context.currency == "USD"
    assert context.store_name == "Allbirds"
    assert context.platform is SourcePlatform.SHOPIFY
    assert context.base_url == STORE


@respx.mock
async def test_discover_tolerates_a_missing_meta_endpoint(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(META_URL).mock(return_value=httpx.Response(404, json={}))
    async with build_client(settings_factory()) as client:
        context = await make_connector(settings_factory, client).discover()
    assert context.currency is None
    assert context.store_name is None


@respx.mock
async def test_duplicate_products_across_pages_are_yielded_once(
    settings_factory: SettingsFactory,
) -> None:
    page_one = fixture("shopify_page_1.json")
    overlap = {"products": [page_one["products"][1], *fixture("shopify_page_2.json")["products"]]}
    route = respx.get(PRODUCTS_URL)
    route.side_effect = [
        json_response(page_one),
        json_response(overlap),
        json_response(fixture("shopify_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(make_connector(settings_factory, client))
    ids = [str(r["id"]) for r in records]
    assert len(ids) == len(set(ids)) == 4


def test_registry_builds_a_shopify_connector(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    client = build_client(settings)
    connector = get_connector(SourcePlatform.SHOPIFY, STORE, settings, client)
    assert isinstance(connector, ShopifyConnector)


def test_registry_rejects_a_platform_without_a_connector(
    settings_factory: SettingsFactory,
) -> None:
    settings = settings_factory()
    client = build_client(settings)
    with pytest.raises(UnsupportedSourceError):
        get_connector(SourcePlatform.WOOCOMMERCE, STORE, settings, client)
