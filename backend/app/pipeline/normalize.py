from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.base import SourceContext
from app.errors import NormalizationError
from app.logging import get_logger, log_event
from app.models.canonical import Attribute, Product, ProductImage, Variant, VariantOption
from app.models.enums import AttributeOrigin, SourcePlatform
from app.pipeline.base import Normalizer
from app.pipeline.html import html_to_text

PLACEHOLDER_OPTION_VALUES = frozenset({"default title"})
PLACEHOLDER_OPTION_NAMES = frozenset({"title"})
MAX_SHOPIFY_OPTIONS = 3

logger = get_logger(__name__)


class ShopifyNormalizer(Normalizer):
    platform = SourcePlatform.SHOPIFY
    requires_llm = False

    async def normalize(self, record: dict[str, Any], context: SourceContext) -> Product:
        return normalize_shopify_product(record, context)

    async def normalize_batch(
        self, records: list[dict[str, Any]], context: SourceContext
    ) -> list[Product]:
        return normalize_shopify_products(records, context)


def normalize_shopify_products(
    records: list[dict[str, Any]], context: SourceContext
) -> list[Product]:
    products: list[Product] = []
    skipped = 0
    for record in records:
        try:
            products.append(normalize_shopify_product(record, context))
        except NormalizationError as exc:
            skipped += 1
            log_event(logger, "skipped unusable record", reason=exc.reason, source_id=exc.source_id)
    if skipped:
        log_event(logger, "normalization finished with skips", kept=len(products), skipped=skipped)
    return products


def normalize_shopify_product(record: dict[str, Any], context: SourceContext) -> Product:
    source_id = str(record.get("id") or "").strip()
    if not source_id:
        raise NormalizationError("", "record has no id")
    title = str(record.get("title") or "").strip()
    if not title:
        raise NormalizationError(source_id, "record has no title")

    option_names = _option_names(record)
    raw_variants = [v for v in record.get("variants") or [] if isinstance(v, dict)]
    variants = [_variant(v, option_names) for v in raw_variants]
    price, compare_at_price = _product_pricing(variants)
    handle = _clean(record.get("handle"))

    return Product(
        source_id=source_id,
        source_platform=SourcePlatform.SHOPIFY,
        source_url=f"{context.base_url}/products/{handle}" if handle else None,
        handle=handle,
        title=title,
        description=html_to_text(record.get("body_html")),
        brand=_clean(record.get("vendor")),
        source_category=_clean(record.get("product_type")),
        price=price,
        currency=context.currency,
        compare_at_price=compare_at_price,
        images=[_image(i) for i in record.get("images") or [] if isinstance(i, dict)],
        attributes=_attributes(raw_variants),
        variants=variants,
        tags=_tags(record.get("tags")),
        fetched_at=context.fetched_at,
        raw=record,
    )


def _option_names(record: dict[str, Any]) -> list[str]:
    options = record.get("options")
    if not isinstance(options, list):
        return []
    names: list[str] = []
    for option in options[:MAX_SHOPIFY_OPTIONS]:
        if isinstance(option, dict):
            names.append(str(option.get("name") or "").strip())
        else:
            names.append("")
    return names


def _variant(record: dict[str, Any], option_names: list[str]) -> Variant:
    featured = record.get("featured_image")
    return Variant(
        source_id=_clean(record.get("id")),
        sku=_clean(record.get("sku")),
        title=_clean(record.get("title")),
        options=_variant_options(record, option_names),
        price=_decimal(record.get("price")),
        compare_at_price=_positive_decimal(record.get("compare_at_price")),
        stock=None,
        available=record.get("available") if isinstance(record.get("available"), bool) else None,
        image=_image(featured) if isinstance(featured, dict) else None,
    )


def _variant_options(record: dict[str, Any], option_names: list[str]) -> list[VariantOption]:
    options: list[VariantOption] = []
    for index in range(MAX_SHOPIFY_OPTIONS):
        value = _clean(record.get(f"option{index + 1}"))
        if not value or value.lower() in PLACEHOLDER_OPTION_VALUES:
            continue
        name = option_names[index] if index < len(option_names) else ""
        if not name or name.lower() in PLACEHOLDER_OPTION_NAMES:
            continue
        options.append(VariantOption(name=name, value=value))
    return options


def _image(record: dict[str, Any]) -> ProductImage:
    return ProductImage(
        url=str(record.get("src") or ""),
        alt=_clean(record.get("alt")),
        position=record.get("position") if isinstance(record.get("position"), int) else None,
        width=record.get("width") if isinstance(record.get("width"), int) else None,
        height=record.get("height") if isinstance(record.get("height"), int) else None,
    )


def _attributes(raw_variants: list[dict[str, Any]]) -> list[Attribute]:
    weights = {
        record.get("grams")
        for record in raw_variants
        if isinstance(record.get("grams"), int) and record.get("grams")
    }
    if len(weights) != 1:
        return []
    return [
        Attribute(
            name="Weight",
            value=str(weights.pop()),
            unit="g",
            origin=AttributeOrigin.SOURCE,
        )
    ]


def _product_pricing(variants: list[Variant]) -> tuple[Decimal | None, Decimal | None]:
    priced = [v for v in variants if v.price is not None]
    if not priced:
        return None, None
    cheapest = min(priced, key=lambda v: v.price or Decimal(0))
    return cheapest.price, cheapest.compare_at_price


def _tags(value: object) -> list[str]:
    if isinstance(value, list):
        candidates = [str(tag) for tag in value]
    elif isinstance(value, str):
        candidates = value.split(",")
    else:
        return []
    return [tag for tag in (c.strip() for c in candidates) if tag]


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None
