from __future__ import annotations

import json
from typing import Any, ClassVar

from app.connectors.base import SourceContext
from app.errors import NormalizationError
from app.llm.client import LLMClient
from app.llm.prompts.loader import render_prompt
from app.logging import get_logger, log_event
from app.models.canonical import Attribute, Product, ProductImage, Variant, VariantOption
from app.models.enums import AttributeOrigin, SourcePlatform
from app.models.llm_io import ColumnMapping, HeaderMapping
from app.pipeline.base import Normalizer
from app.pipeline.html import html_to_text
from app.pipeline.values import clean, decimal_or_none, positive_decimal

HEADER_STAGE = "map_headers"
SAMPLE_VALUES = 3
IMAGE_SEPARATORS = (",", "|", ";")
TAG_SEPARATORS = (",", "|", ";")
SINGLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "brand",
        "source_category",
        "price",
        "compare_at_price",
        "currency",
        "sku",
        "handle",
        "source_url",
        "stock",
        "option_name",
        "option_value",
    }
)

logger = get_logger(__name__)


class SpreadsheetNormalizer(Normalizer):
    platform: ClassVar[SourcePlatform] = SourcePlatform.CSV
    requires_llm: ClassVar[bool] = True

    def __init__(self, llm: LLMClient, mapping: HeaderMapping | None = None) -> None:
        self.llm = llm
        self.mapping = mapping

    async def learn_headers(
        self, headers: list[str], sample: list[dict[str, str]], model: str | None = None
    ) -> HeaderMapping:
        mapping, _ = await self.llm.complete_json(
            prompt=render_prompt("map_headers", columns=_render_columns(headers, sample)),
            response_model=HeaderMapping,
            stage=HEADER_STAGE,
            model=model,
        )
        self.mapping = _reconcile(mapping, headers)
        return self.mapping

    async def normalize(self, record: dict[str, Any], context: SourceContext) -> Product:
        if self.mapping is None:
            raise NormalizationError("", "learn_headers must run before normalize")
        return build_product(record, self.mapping, context)

    async def normalize_batch(
        self, records: list[dict[str, Any]], context: SourceContext
    ) -> list[Product]:
        if self.mapping is None:
            raise NormalizationError("", "learn_headers must run before normalize")
        products: list[Product] = []
        skipped = 0
        for index, record in enumerate(records, start=1):
            try:
                products.append(build_product(record, self.mapping, context, row=index))
            except NormalizationError as exc:
                skipped += 1
                log_event(logger, "skipped unusable row", reason=exc.reason, row=exc.source_id)
        if skipped:
            log_event(
                logger, "normalization finished with skips", kept=len(products), skipped=skipped
            )
        return products


def build_product(
    record: dict[str, Any],
    mapping: HeaderMapping,
    context: SourceContext,
    row: int = 0,
) -> Product:
    by_field = _by_field(mapping)
    title = _first(record, by_field.get("title", []))
    if not title:
        raise NormalizationError(str(row), "row has no title")

    sku = _first(record, by_field.get("sku", []))
    handle = _first(record, by_field.get("handle", []))
    price = decimal_or_none(_first(record, by_field.get("price", [])))
    compare_at = positive_decimal(_first(record, by_field.get("compare_at_price", [])))
    option_name = _first(record, by_field.get("option_name", []))
    option_value = _first(record, by_field.get("option_value", []))
    stock = _int(_first(record, by_field.get("stock", [])))

    return Product(
        source_id=sku or handle or f"row-{row}",
        source_platform=context.platform,
        source_url=_first(record, by_field.get("source_url", [])),
        handle=handle,
        title=title,
        description=html_to_text(_first(record, by_field.get("description", []))),
        brand=_first(record, by_field.get("brand", [])),
        source_category=_first(record, by_field.get("source_category", [])),
        price=price,
        currency=_currency(record, by_field),
        compare_at_price=compare_at,
        images=_images(record, by_field.get("image", [])),
        attributes=_attributes(record, mapping),
        variants=[
            Variant(
                sku=sku,
                title=option_value,
                options=(
                    [VariantOption(name=option_name, value=option_value)]
                    if option_name and option_value
                    else []
                ),
                price=price,
                compare_at_price=compare_at,
                stock=stock,
                available=None if stock is None else stock > 0,
            )
        ],
        tags=_tags(record, by_field.get("tags", [])),
        fetched_at=context.fetched_at,
        raw=dict(record),
    )


def _by_field(mapping: HeaderMapping) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for entry in mapping.mappings:
        grouped.setdefault(entry.canonical_field, []).append(entry.column)
    return grouped


def _first(record: dict[str, Any], columns: list[str]) -> str | None:
    for column in columns:
        value = clean(record.get(column))
        if value:
            return value
    return None


def _currency(record: dict[str, Any], by_field: dict[str, list[str]]) -> str | None:
    value = _first(record, by_field.get("currency", []))
    if value and len(value) == 3 and value.isalpha():
        return value.upper()
    return None


def _int(value: str | None) -> int | None:
    parsed = decimal_or_none(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (ValueError, ArithmeticError):
        return None


def _images(record: dict[str, Any], columns: list[str]) -> list[ProductImage]:
    images: list[ProductImage] = []
    for column in columns:
        raw = clean(record.get(column))
        if not raw:
            continue
        for part in _split(raw, IMAGE_SEPARATORS):
            if part.startswith(("http://", "https://", "//")):
                images.append(ProductImage(url=part, position=len(images) + 1))
    return images


def _tags(record: dict[str, Any], columns: list[str]) -> list[str]:
    tags: list[str] = []
    for column in columns:
        raw = clean(record.get(column))
        if raw:
            tags.extend(_split(raw, TAG_SEPARATORS))
    return list(dict.fromkeys(tags))


def _attributes(record: dict[str, Any], mapping: HeaderMapping) -> list[Attribute]:
    attributes: list[Attribute] = []
    for entry in mapping.mappings:
        if entry.canonical_field != "attribute":
            continue
        value = clean(record.get(entry.column))
        if not value:
            continue
        attributes.append(
            Attribute(
                name=entry.attribute_name or entry.column,
                value=value,
                unit=entry.unit,
                origin=AttributeOrigin.SOURCE,
            )
        )
    return attributes


def _split(raw: str, separators: tuple[str, ...]) -> list[str]:
    parts = [raw]
    for separator in separators:
        if separator in raw:
            parts = raw.split(separator)
            break
    return [part.strip() for part in parts if part.strip()]


def _reconcile(mapping: HeaderMapping, headers: list[str]) -> HeaderMapping:
    known = set(headers)
    seen: set[str] = set()
    kept: list[ColumnMapping] = []
    for entry in mapping.mappings:
        if entry.column not in known:
            log_event(logger, "dropped a mapping for an unknown column", column=entry.column)
            continue
        if entry.column in seen:
            log_event(logger, "dropped a duplicate column mapping", column=entry.column)
            continue
        if entry.canonical_field == "ignore":
            seen.add(entry.column)
            continue
        if entry.canonical_field not in SINGLE_FIELDS and entry.canonical_field not in {
            "attribute",
            "image",
            "tags",
        }:
            log_event(
                logger,
                "dropped a mapping to an unknown canonical field",
                column=entry.column,
                field=entry.canonical_field,
            )
            continue
        seen.add(entry.column)
        kept.append(entry)
    missing = [header for header in headers if header not in seen]
    return HeaderMapping(
        mappings=kept,
        unmapped_columns=sorted(set(mapping.unmapped_columns) | set(missing)),
        notes=mapping.notes,
    )


def _render_columns(headers: list[str], sample: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for header in headers:
        values = [row.get(header, "") for row in sample]
        shown = [value for value in values if value][:SAMPLE_VALUES]
        rendered = json.dumps(shown, ensure_ascii=False) if shown else "[all blank]"
        lines.append(f"- {header}: {rendered}")
    return "\n".join(lines)
