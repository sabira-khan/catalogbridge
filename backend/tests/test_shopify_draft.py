from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx
from app.connectors.registry import build_client
from app.errors import ShopifyAdminError
from app.models.canonical import Product, ProductImage, Variant, VariantOption
from app.models.enums import SourcePlatform
from app.models.mapping import MappedAttribute, MappedProduct
from app.pipeline.shopify_draft import ShopifyDraftLoader, draft_payload
from app.pipeline.targets import load_target_specs

from tests.conftest import SettingsFactory

SHOP = "demo-store.myshopify.com"
ENDPOINT = f"https://{SHOP}/admin/api/2026-01/products.json"


def mapped(*, needs_review: bool = False) -> MappedProduct:
    product = Product(
        source_id="1",
        source_platform=SourcePlatform.SHOPIFY,
        title="Merino Crew Jumper",
        brand="Northfield",
        description="A fine gauge merino jumper.\n\nMade in Portugal.",
        price=Decimal("89.00"),
        currency="GBP",
        images=[ProductImage(url="https://cdn.example.com/jumper.jpg")],
        tags=["knitwear"],
        variants=[
            Variant(
                sku="MCJ-S-NAVY",
                options=[
                    VariantOption(name="Size", value="S"),
                    VariantOption(name="Colour", value="Navy"),
                ],
                price=Decimal("89.00"),
                compare_at_price=Decimal("120.00"),
            ),
            Variant(
                sku="MCJ-M-NAVY",
                options=[
                    VariantOption(name="Size", value="M"),
                    VariantOption(name="Colour", value="Navy"),
                ],
                price=Decimal("89.00"),
            ),
        ],
    )
    return MappedProduct(
        product=product,
        target_category_id="w_knitwear",
        target_category_path=["Women", "Tops", "Knitwear"],
        category_confidence=0.93,
        category_reason="knitwear",
        mapped_attributes=[
            MappedAttribute(target_key="material", value="merino wool"),
            MappedAttribute(target_key="weight", value="340", unit="g"),
        ],
        needs_review=needs_review,
    )


def loader(settings_factory: SettingsFactory, client: httpx.AsyncClient) -> ShopifyDraftLoader:
    settings = settings_factory()
    settings.connector_page_delay = 0.0
    settings.connector_backoff_base = 0.0
    return ShopifyDraftLoader(SHOP, "shpat_test_token", settings, client)


def test_payload_is_always_a_draft() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    assert payload["status"] == "draft"
    assert payload["published"] is False


def test_payload_carries_options_and_variants() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    assert [o["name"] for o in payload["options"]] == ["Size", "Colour"]
    assert payload["variants"][0]["option1"] == "S"
    assert payload["variants"][0]["option2"] == "Navy"
    assert payload["variants"][0]["sku"] == "MCJ-S-NAVY"
    assert payload["variants"][0]["compare_at_price"] == "120.00"
    assert "compare_at_price" not in payload["variants"][1]


def test_description_becomes_paragraph_html() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    assert payload["body_html"] == (
        "<p>A fine gauge merino jumper.</p><p>Made in Portugal.</p>"
    )


def test_html_in_the_description_is_escaped() -> None:
    item = mapped()
    item.product.description = "5 < 6 & <script>alert(1)</script>"
    payload = draft_payload(item, load_target_specs()[0])
    assert "<script>" not in payload["body_html"]
    assert "&lt;script&gt;" in payload["body_html"]


def test_category_path_becomes_the_product_type() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    assert payload["product_type"] == "Women > Tops > Knitwear"


def test_tags_record_the_target_and_category() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    tags = payload["tags"].split(", ")
    assert "knitwear" in tags
    assert "catalogbridge:meridian_apparel" in tags
    assert "catalogbridge:w_knitwear" in tags
    assert "catalogbridge:needs-review" not in tags


def test_a_flagged_product_is_tagged_for_review() -> None:
    payload = draft_payload(mapped(needs_review=True), load_target_specs()[0])
    assert "catalogbridge:needs-review" in payload["tags"].split(", ")


def test_attributes_become_metafields_with_their_units() -> None:
    payload = draft_payload(mapped(), load_target_specs()[0])
    values = {f["key"]: f["value"] for f in payload["metafields"]}
    assert values == {"material": "merino wool", "weight": "340 g"}
    assert all(f["namespace"] == "catalogbridge" for f in payload["metafields"])


def test_a_product_with_no_variants_still_gets_one() -> None:
    item = mapped()
    item.product.variants = []
    payload = draft_payload(item, load_target_specs()[0])
    assert len(payload["variants"]) == 1
    assert payload["variants"][0]["price"] == "0.00"
    assert "options" not in payload


def test_a_token_is_required(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    client = build_client(settings)
    with pytest.raises(ShopifyAdminError, match="access token is required"):
        ShopifyDraftLoader(SHOP, "", settings, client)


@respx.mock
async def test_drafts_are_created_and_the_token_is_sent(
    settings_factory: SettingsFactory,
) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(201, json={"product": {"id": 999}})
    )
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push([mapped()], load_target_specs()[0])
    assert result["created"] == ["999"]
    assert route.calls[0].request.headers["X-Shopify-Access-Token"] == "shpat_test_token"
    body = json.loads(route.calls[0].request.content)
    assert body["product"]["status"] == "draft"


@respx.mock
async def test_flagged_products_are_skipped_by_default(
    settings_factory: SettingsFactory,
) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(201, json={"product": {"id": 1}})
    )
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push(
            [mapped(needs_review=True)], load_target_specs()[0]
        )
    assert result["created"] == []
    assert result["skipped"] == ["Merino Crew Jumper"]
    assert route.call_count == 0


@respx.mock
async def test_flagged_products_can_be_pushed_deliberately(
    settings_factory: SettingsFactory,
) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(201, json={"product": {"id": 2}}))
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push(
            [mapped(needs_review=True)], load_target_specs()[0], skip_flagged=False
        )
    assert result["created"] == ["2"]


@respx.mock
async def test_rate_limit_is_retried(settings_factory: SettingsFactory) -> None:
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(201, json={"product": {"id": 3}}),
    ]
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push([mapped()], load_target_specs()[0])
    assert result["created"] == ["3"]
    assert route.call_count == 2


@respx.mock
async def test_a_bad_token_is_reported_clearly(settings_factory: SettingsFactory) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401, json={}))
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push([mapped()], load_target_specs()[0])
    assert result["created"] == []
    assert "rejected the access token" in result["failed"][0]["error"]


@respx.mock
async def test_one_failure_does_not_stop_the_batch(settings_factory: SettingsFactory) -> None:
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(422, json={"errors": "title is invalid"}),
        httpx.Response(201, json={"product": {"id": 5}}),
    ]
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push(
            [mapped(), mapped()], load_target_specs()[0]
        )
    assert result["created"] == ["5"]
    assert len(result["failed"]) == 1


@respx.mock
async def test_an_unexpected_response_shape_is_reported(
    settings_factory: SettingsFactory,
) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(201, json={"ok": True}))
    async with build_client(settings_factory()) as client:
        result = await loader(settings_factory, client).push([mapped()], load_target_specs()[0])
    assert "unexpected Shopify response" in result["failed"][0]["error"]


def test_the_shop_domain_is_normalised(settings_factory: SettingsFactory) -> None:
    settings = settings_factory()
    client = build_client(settings)
    instance = ShopifyDraftLoader(f"https://{SHOP}/", "token", settings, client)
    assert instance.endpoint == ENDPOINT
