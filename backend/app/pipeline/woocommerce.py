from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.connectors.base import SourceContext
from app.errors import NormalizationError
from app.logging import get_logger, log_event
from app.models.canonical import Attribute, Product, ProductImage, Variant, VariantOption
from app.models.enums import AttributeOrigin, SourcePlatform
from app.pipeline.base import Normalizer
from app.pipeline.html import html_to_text
from app.pipeline.values import clean, from_minor_units

logger = get_logger(__name__)


class WooCommerceNormalizer(Normalizer):
    platform = SourcePlatform.WOOCOMMERCE
    requires_llm = False

    async def normalize(self, record: dict[str, Any], context: SourceContext) -> Product:
        return normalize_woocommerce_product(record, context)

    async def normalize_batch(
        self, records: list[dict[str, Any]], context: SourceContext
    ) -> list[Product]:
        return normalize_woocommerce_products(records, context)


def normalize_woocommerce_products(
    records: list[dict[str, Any]], context: SourceContext
) -> list[Product]:
    products: list[Product] = []
    skipped = 0
    for record in records:
        try:
            products.append(normalize_woocommerce_product(record, context))
        except NormalizationError as exc:
            skipped += 1
            log_event(logger, "skipped unusable record", reason=exc.reason, source_id=exc.source_id)
    if skipped:
        log_event(logger, "normalization finished with skips", kept=len(products), skipped=skipped)
    return products


def normalize_woocommerce_product(record: dict[str, Any], context: SourceContext) -> Product:
    source_id = str(record.get("id") or "").strip()
    if not source_id:
        raise NormalizationError("", "record has no id")
    title = str(record.get("name") or "").strip()
    if not title:
        raise NormalizationError(source_id, "record has no name")

    raw_prices = record.get("prices")
    prices: dict[str, Any] = raw_prices if isinstance(raw_prices, dict) else {}
    minor_unit = prices.get("currency_minor_unit", 2)
    price = from_minor_units(prices.get("price"), minor_unit)
    regular = from_minor_units(prices.get("regular_price"), minor_unit)
    currency = clean(prices.get("currency_code")) or context.currency

    return Product(
        source_id=source_id,
        source_platform=SourcePlatform.WOOCOMMERCE,
        source_url=clean(record.get("permalink")),
        handle=clean(record.get("slug")),
        title=title,
        description=html_to_text(record.get("description"))
        or html_to_text(record.get("short_description")),
        brand=_brand(record),
        source_category=_primary_category(record),
        price=price,
        currency=currency,
        compare_at_price=_compare_at(price, regular),
        images=[_image(i) for i in record.get("images") or [] if isinstance(i, dict)],
        attributes=_attributes(record),
        variants=_variants(record, price, currency),
        tags=_names(record.get("tags")),
        fetched_at=context.fetched_at,
        raw=record,
    )


def _compare_at(price: Decimal | None, regular: Decimal | None) -> Decimal | None:
    if price is None or regular is None or regular <= price:
        return None
    return regular


def _brand(record: dict[str, Any]) -> str | None:
    for attribute in record.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = str(attribute.get("name") or "").strip().lower()
        if name in {"brand", "marke", "marque"}:
            terms = _names(attribute.get("terms"))
            if terms:
                return terms[0]
    return None


def _primary_category(record: dict[str, Any]) -> str | None:
    names = _names(record.get("categories"))
    return names[0] if names else None


def _image(record: dict[str, Any]) -> ProductImage:
    return ProductImage(
        url=str(record.get("src") or ""),
        alt=clean(record.get("alt")),
        position=None,
        width=None,
        height=None,
    )


def _attributes(record: dict[str, Any]) -> list[Attribute]:
    attributes: list[Attribute] = []
    for attribute in record.get("attributes") or []:
        if not isinstance(attribute, dict) or attribute.get("has_variations"):
            continue
        name = clean(attribute.get("name"))
        terms = _names(attribute.get("terms"))
        if not name or not terms:
            continue
        attributes.append(
            Attribute(name=name, value=", ".join(terms), origin=AttributeOrigin.SOURCE)
        )
    return attributes


def _variants(
    record: dict[str, Any], price: Decimal | None, currency: str | None
) -> list[Variant]:
    variations = [v for v in record.get("variations") or [] if isinstance(v, dict)]
    sku = clean(record.get("sku"))
    in_stock = record.get("is_in_stock")
    available = in_stock if isinstance(in_stock, bool) else None
    if not variations:
        return [
            Variant(
                source_id=str(record.get("id")),
                sku=sku,
                title=None,
                options=[],
                price=price,
                stock=_low_stock(record),
                available=available,
            )
        ]
    return [
        Variant(
            source_id=clean(variation.get("id")),
            sku=None,
            title=None,
            options=_options(variation.get("attributes")),
            price=price,
            stock=None,
            available=available,
        )
        for variation in variations
    ]


def _options(raw: object) -> list[VariantOption]:
    options: list[VariantOption] = []
    if not isinstance(raw, list):
        return options
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = clean(item.get("name"))
        value = clean(item.get("value"))
        if name and value:
            options.append(VariantOption(name=name, value=value))
    return options


def _low_stock(record: dict[str, Any]) -> int | None:
    remaining = record.get("low_stock_remaining")
    return remaining if isinstance(remaining, int) else None


def _names(raw: object) -> list[str]:
    names: list[str] = []
    if not isinstance(raw, list):
        return names
    for item in raw:
        name = clean(item.get("name")) if isinstance(item, dict) else clean(item)
        if name:
            names.append(name)
    return names
