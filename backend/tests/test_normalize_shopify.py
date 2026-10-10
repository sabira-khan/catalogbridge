from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from app.connectors.base import SourceContext
from app.errors import NormalizationError
from app.models.enums import AttributeOrigin, SourcePlatform
from app.pipeline.html import html_to_text
from app.pipeline.normalize import normalize_shopify_product, normalize_shopify_products

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def context(currency: str | None = "USD") -> SourceContext:
    return SourceContext(
        platform=SourcePlatform.SHOPIFY,
        base_url="https://examplestore.com",
        currency=currency,
        store_name="Example Store",
        fetched_at=FETCHED_AT,
    )


def edge(index: int) -> dict[str, Any]:
    return fixture("shopify_edge_cases.json")["products"][index]


def test_real_product_is_normalized_end_to_end() -> None:
    record = fixture("shopify_page_1.json")["products"][0]
    product = normalize_shopify_product(record, context())
    assert product.source_id == str(record["id"])
    assert product.source_platform is SourcePlatform.SHOPIFY
    assert product.title == record["title"]
    assert product.brand == record["vendor"]
    assert product.source_category == record["product_type"]
    assert product.currency == "USD"
    assert product.source_url == f"https://examplestore.com/products/{record['handle']}"
    assert product.fetched_at == FETCHED_AT
    assert product.raw == record
    assert len(product.variants) == len(record["variants"])


def test_description_is_plain_text_and_html_survives_in_raw() -> None:
    product = normalize_shopify_product(edge(0), context())
    assert product.description is not None
    assert "<" not in product.description
    assert "Café blend, 100% arabica." in product.description
    assert "Dark roast" in product.description
    assert product.raw["body_html"].startswith("<p>")


def test_missing_body_html_gives_no_description() -> None:
    assert normalize_shopify_product(edge(1), context()).description is None


def test_default_title_option_is_dropped() -> None:
    product = normalize_shopify_product(edge(0), context())
    assert product.variants[0].options == []
    assert len(product.variants) == 1


def test_real_options_are_paired_with_their_names() -> None:
    product = normalize_shopify_product(edge(1), context())
    pairs = [(o.name, o.value) for o in product.variants[0].options]
    assert pairs == [("Size", "Small"), ("Color", "Red")]


def test_price_comes_from_the_cheapest_variant() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert product.price == Decimal("11.25")


def test_compare_at_price_follows_the_cheapest_variant() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert product.compare_at_price is None


def test_zero_compare_at_price_is_treated_as_absent() -> None:
    product = normalize_shopify_product(edge(0), context())
    assert product.price == Decimal("9.50")
    assert product.compare_at_price is None
    assert product.variants[0].compare_at_price is None


def test_empty_vendor_and_product_type_become_none() -> None:
    product = normalize_shopify_product(edge(0), context())
    assert product.brand is None
    assert product.source_category is None


def test_title_is_stripped() -> None:
    assert normalize_shopify_product(edge(0), context()).title == "Unnamed Single Variant"


def test_protocol_relative_image_is_absolutized() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert product.images[0].url == "https://cdn.shopify.com/s/files/1/mug.jpg"
    assert product.images[0].width == 1200


def test_product_without_images_is_allowed() -> None:
    assert normalize_shopify_product(edge(0), context()).images == []


def test_stock_is_never_invented_but_availability_is_kept() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert [v.stock for v in product.variants] == [None, None]
    assert [v.available for v in product.variants] == [True, False]


def test_shared_variant_weight_becomes_a_source_attribute() -> None:
    record = fixture("shopify_page_1.json")["products"][0]
    for variant in record["variants"]:
        variant["grams"] = 455
    product = normalize_shopify_product(record, context())
    assert len(product.attributes) == 1
    weight = product.attributes[0]
    assert (weight.name, weight.value, weight.unit) == ("Weight", "455", "g")
    assert weight.origin is AttributeOrigin.SOURCE


def test_differing_variant_weights_emit_no_product_attribute() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert product.attributes == []


def test_zero_weight_emits_no_attribute() -> None:
    assert normalize_shopify_product(edge(0), context()).attributes == []


def test_tags_are_normalized_from_a_list() -> None:
    product = normalize_shopify_product(edge(1), context())
    assert product.tags == ["drinkware", "ceramic"]


def test_tags_are_normalized_from_a_comma_string() -> None:
    record = {**edge(1), "tags": " shoes , running ,, "}
    assert normalize_shopify_product(record, context()).tags == ["shoes", "running"]


def test_missing_currency_is_left_unset() -> None:
    assert normalize_shopify_product(edge(1), context(None)).currency is None


def test_record_without_id_is_rejected() -> None:
    with pytest.raises(NormalizationError):
        normalize_shopify_product({"title": "No id"}, context())


def test_record_without_title_is_rejected() -> None:
    with pytest.raises(NormalizationError):
        normalize_shopify_product({"id": 1}, context())


def test_batch_skips_unusable_records_and_keeps_the_rest() -> None:
    records = [edge(0), {"title": "broken"}, edge(1)]
    products = normalize_shopify_products(records, context())
    assert len(products) == 2
    assert [p.source_id for p in products] == ["900000000001", "900000000002"]


def test_every_normalized_product_survives_a_json_round_trip() -> None:
    records = [
        *fixture("shopify_page_1.json")["products"],
        *fixture("shopify_page_2.json")["products"],
    ]
    for product in normalize_shopify_products(records, context()):
        assert product.model_dump(mode="json")["source_id"] == product.source_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("<p>Hello</p>", "Hello"),
        ("<p>One</p><p>Two</p>", "One\n\nTwo"),
        ("Caf&eacute; &amp; cream", "Café & cream"),
        ("<ul><li>A</li><li>B</li></ul>", "A\n\nB"),
        ("<div>Keep</div><script>alert(1)</script>", "Keep"),
        ("<p>Line<br>Break</p>", "Line\nBreak"),
        ("<p>  spaced   out  </p>", "spaced out"),
    ],
)
def test_html_to_text(raw: str | None, expected: str | None) -> None:
    assert html_to_text(raw) == expected
