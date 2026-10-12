from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from app.connectors.base import SourceContext
from app.connectors.registry import build_client, get_connector
from app.connectors.woocommerce import WooCommerceConnector
from app.errors import ConnectorHTTPError, NormalizationError, StoreNotWooCommerceError
from app.models.enums import SourcePlatform
from app.pipeline.registry import get_normalizer
from app.pipeline.woocommerce import (
    WooCommerceNormalizer,
    normalize_woocommerce_product,
    normalize_woocommerce_products,
)

from tests.conftest import SettingsFactory

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORE = "https://examplewoo.com"
V1 = f"{STORE}/wp-json/wc/store/v1/products"
LEGACY = f"{STORE}/wp-json/wc/store/products"
SITE = f"{STORE}/wp-json"
FETCHED_AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def context(currency: str | None = "GBP") -> SourceContext:
    return SourceContext(
        platform=SourcePlatform.WOOCOMMERCE,
        base_url=STORE,
        currency=currency,
        store_name="Example Woo Store",
        fetched_at=FETCHED_AT,
    )


def connector(settings_factory: SettingsFactory, client: httpx.AsyncClient) -> WooCommerceConnector:
    settings = settings_factory()
    settings.connector_page_delay = 0.0
    settings.connector_backoff_base = 0.0
    return WooCommerceConnector(STORE, settings, client)


async def collect(instance: WooCommerceConnector) -> list[dict[str, Any]]:
    return [record async for record in instance.iter_records()]


@respx.mock
async def test_pagination_walks_the_store_api(settings_factory: SettingsFactory) -> None:
    route = respx.get(V1)
    route.side_effect = [
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(200, json=fixture("woo_page_2.json")),
        httpx.Response(200, json=fixture("woo_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert [r["id"] for r in records] == [101, 102, 103]
    assert route.call_count == 3


@respx.mock
async def test_per_page_is_capped_at_the_store_api_limit(
    settings_factory: SettingsFactory,
) -> None:
    route = respx.get(V1)
    route.side_effect = [
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(200, json=fixture("woo_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        await collect(connector(settings_factory, client))
    assert route.calls[0].request.url.params["per_page"] == "100"


@respx.mock
async def test_legacy_store_api_path_is_tried_when_v1_is_absent(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(V1).mock(return_value=httpx.Response(404, json={"code": "rest_no_route"}))
    legacy = respx.get(LEGACY)
    legacy.side_effect = [
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(200, json=fixture("woo_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert len(records) == 2
    assert legacy.call_count == 2


@respx.mock
async def test_the_working_path_is_remembered(settings_factory: SettingsFactory) -> None:
    v1 = respx.get(V1).mock(return_value=httpx.Response(404, json={}))
    legacy = respx.get(LEGACY)
    legacy.side_effect = [
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(200, json=fixture("woo_page_2.json")),
        httpx.Response(200, json=fixture("woo_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        await collect(connector(settings_factory, client))
    assert v1.call_count == 1
    assert legacy.call_count == 3


@respx.mock
async def test_a_site_without_the_store_api_is_rejected(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(V1).mock(return_value=httpx.Response(404, json={}))
    respx.get(LEGACY).mock(return_value=httpx.Response(404, json={}))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotWooCommerceError):
            await collect(connector(settings_factory, client))


@respx.mock
async def test_an_html_response_is_rejected(settings_factory: SettingsFactory) -> None:
    respx.get(V1).mock(return_value=httpx.Response(200, html="<html></html>"))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotWooCommerceError):
            await collect(connector(settings_factory, client))


@respx.mock
async def test_an_object_instead_of_an_array_is_rejected(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(V1).mock(return_value=httpx.Response(200, json={"products": []}))
    async with build_client(settings_factory()) as client:
        with pytest.raises(StoreNotWooCommerceError):
            await collect(connector(settings_factory, client))


@respx.mock
async def test_rate_limit_is_retried(settings_factory: SettingsFactory) -> None:
    route = respx.get(V1)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(200, json=fixture("woo_page_empty.json")),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert len(records) == 2


@respx.mock
async def test_persistent_server_error_raises(settings_factory: SettingsFactory) -> None:
    respx.get(V1).mock(return_value=httpx.Response(503))
    async with build_client(settings_factory()) as client:
        with pytest.raises(ConnectorHTTPError):
            await collect(connector(settings_factory, client))


@respx.mock
async def test_page_beyond_the_last_is_treated_as_the_end(
    settings_factory: SettingsFactory,
) -> None:
    route = respx.get(V1)
    route.side_effect = [
        httpx.Response(200, json=fixture("woo_page_1.json")),
        httpx.Response(400, json={"code": "woocommerce_rest_invalid_page"}),
    ]
    async with build_client(settings_factory()) as client:
        records = await collect(connector(settings_factory, client))
    assert len(records) == 2


@respx.mock
async def test_discover_reads_currency_and_store_name(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(V1).mock(return_value=httpx.Response(200, json=fixture("woo_page_1.json")))
    respx.get(SITE).mock(return_value=httpx.Response(200, json=fixture("woo_site_info.json")))
    async with build_client(settings_factory()) as client:
        found = await connector(settings_factory, client).discover()
    assert found.currency == "GBP"
    assert found.store_name == "Example Woo Store"
    assert found.platform is SourcePlatform.WOOCOMMERCE


@respx.mock
async def test_discover_tolerates_a_missing_site_info_endpoint(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(V1).mock(return_value=httpx.Response(200, json=fixture("woo_page_1.json")))
    respx.get(SITE).mock(return_value=httpx.Response(404, json={}))
    async with build_client(settings_factory()) as client:
        found = await connector(settings_factory, client).discover()
    assert found.store_name is None
    assert found.currency == "GBP"


def test_minor_units_become_real_decimal_prices() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert product.price == Decimal("45.00")
    assert product.compare_at_price == Decimal("55.00")
    assert product.currency == "GBP"


def test_equal_regular_price_is_not_a_discount() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[1], context())
    assert product.price == Decimal("189.00")
    assert product.compare_at_price is None


def test_zero_minor_unit_currency_is_handled() -> None:
    product = normalize_woocommerce_product(fixture("woo_edge_cases.json")[0], context())
    assert product.price == Decimal("1500")
    assert product.currency == "JPY"


def test_variations_become_variants_with_options() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert len(product.variants) == 2
    assert [o.value for v in product.variants for o in v.options] == ["S", "M"]
    assert all(v.options[0].name == "Size" for v in product.variants)


def test_a_simple_product_still_gets_one_variant() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[1], context())
    assert len(product.variants) == 1
    assert product.variants[0].sku == "SDJ-001"
    assert product.variants[0].available is False


def test_low_stock_remaining_becomes_stock() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_2.json")[0], context())
    assert product.variants[0].stock == 3


def test_brand_is_read_from_a_non_variation_attribute() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert product.brand == "Northfield"


def test_variation_attributes_are_not_copied_into_product_attributes() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    names = {a.name for a in product.attributes}
    assert "Material" in names
    assert "Size" not in names


def test_brand_attribute_with_no_terms_is_ignored() -> None:
    product = normalize_woocommerce_product(fixture("woo_edge_cases.json")[0], context())
    assert product.brand is None
    assert product.attributes == []


def test_first_category_becomes_the_source_category() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert product.source_category == "T-shirts"


def test_description_falls_back_to_short_description() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[1], context())
    assert product.description == "14oz selvedge denim, unwashed."


def test_html_is_stripped_from_the_description() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert product.description is not None
    assert "<" not in product.description
    assert "Garment dyed" in product.description


def test_protocol_relative_image_is_absolutized() -> None:
    product = normalize_woocommerce_product(fixture("woo_edge_cases.json")[0], context())
    assert product.images[0].url == "https://examplewoo.com/wp/protocol-relative.jpg"


def test_tags_become_plain_strings() -> None:
    product = normalize_woocommerce_product(fixture("woo_page_1.json")[0], context())
    assert product.tags == ["organic", "cotton"]


def test_title_is_stripped() -> None:
    product = normalize_woocommerce_product(fixture("woo_edge_cases.json")[0], context())
    assert product.title == "Zero Minor Unit Currency Item"


def test_record_without_id_is_rejected() -> None:
    with pytest.raises(NormalizationError):
        normalize_woocommerce_product(fixture("woo_edge_cases.json")[1], context())


def test_record_without_name_is_rejected() -> None:
    with pytest.raises(NormalizationError):
        normalize_woocommerce_product(fixture("woo_edge_cases.json")[2], context())


def test_batch_skips_unusable_records() -> None:
    products = normalize_woocommerce_products(fixture("woo_edge_cases.json"), context())
    assert [p.source_id for p in products] == ["104"]


def test_registries_resolve_woocommerce(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    client = build_client(settings)
    found = get_connector(SourcePlatform.WOOCOMMERCE, STORE, settings, client)
    assert isinstance(found, WooCommerceConnector)
    assert isinstance(get_normalizer(SourcePlatform.WOOCOMMERCE), WooCommerceNormalizer)
