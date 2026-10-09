from __future__ import annotations

from decimal import Decimal

import pytest
from app.models import (
    Attribute,
    AttributeOrigin,
    Product,
    ProductImage,
    SourcePlatform,
    Variant,
    VariantOption,
)
from pydantic import ValidationError


def _product(**overrides: object) -> Product:
    defaults: dict[str, object] = {
        "source_id": "123",
        "source_platform": SourcePlatform.SHOPIFY,
        "title": "Merino Runner",
    }
    return Product(**{**defaults, **overrides})


def test_protocol_relative_image_url_is_absolutized() -> None:
    image = ProductImage(url="//cdn.shopify.com/s/files/1/shoe.jpg")
    assert image.url == "https://cdn.shopify.com/s/files/1/shoe.jpg"


def test_absolute_image_url_is_untouched() -> None:
    image = ProductImage(url="https://cdn.shopify.com/s/files/1/shoe.jpg")
    assert image.url == "https://cdn.shopify.com/s/files/1/shoe.jpg"


def test_currency_is_upcased() -> None:
    assert _product(currency="kwd").currency == "KWD"


def test_currency_rejects_wrong_length() -> None:
    with pytest.raises(ValidationError):
        _product(currency="KWDX")


def test_price_is_derived_from_cheapest_variant() -> None:
    product = _product(
        variants=[
            Variant(sku="A", price=Decimal("129.00")),
            Variant(sku="B", price=Decimal("98.50")),
            Variant(sku="C", price=None),
        ]
    )
    assert product.price == Decimal("98.50")


def test_explicit_price_is_not_overwritten() -> None:
    product = _product(
        price=Decimal("75.00"),
        variants=[Variant(sku="A", price=Decimal("129.00"))],
    )
    assert product.price == Decimal("75.00")


def test_price_stays_none_without_variant_prices() -> None:
    assert _product(variants=[Variant(sku="A")]).price is None


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _product(colour="red")


def test_attribute_defaults_to_source_origin() -> None:
    attribute = Attribute(name="Material", value="merino wool")
    assert attribute.origin is AttributeOrigin.SOURCE
    assert attribute.unit is None


def test_json_round_trip_preserves_the_product() -> None:
    product = _product(
        currency="USD",
        images=[ProductImage(url="//cdn.shopify.com/a.jpg", position=1)],
        attributes=[Attribute(name="Weight", value="250", unit="g", origin=AttributeOrigin.LLM)],
        variants=[
            Variant(
                sku="MR-42-BLK",
                options=[
                    VariantOption(name="Size", value="42"),
                    VariantOption(name="Color", value="Black"),
                ],
                price=Decimal("129.00"),
                stock=4,
                available=True,
            )
        ],
        tags=["shoes", "running"],
        raw={"id": 123, "body_html": "<p>hi</p>"},
    )
    restored = Product.model_validate(product.model_dump(mode="json"))
    assert restored == product
    assert restored.variants[0].price == Decimal("129.00")


def test_for_llm_drops_provenance_and_raw() -> None:
    product = _product(raw={"id": 123}, currency="USD", brand="Allbirds")
    payload = product.for_llm()
    assert "raw" not in payload
    assert "source_id" not in payload
    assert "source_platform" not in payload
    assert payload["title"] == "Merino Runner"
    assert payload["brand"] == "Allbirds"


def test_strings_are_stripped() -> None:
    assert _product(title="  Merino Runner  ").title == "Merino Runner"
