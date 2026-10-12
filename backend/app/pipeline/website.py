from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, ClassVar

from app.connectors.base import SourceContext
from app.errors import NormalizationError
from app.llm.client import LLMClient
from app.llm.prompts.loader import render_prompt
from app.logging import get_logger, log_event
from app.models.canonical import Attribute, Product, ProductImage, Variant, VariantOption
from app.models.enums import AttributeOrigin, SourcePlatform
from app.models.llm_io import ProductDraft
from app.pipeline.base import Normalizer
from app.pipeline.html import html_to_text
from app.pipeline.values import clean, decimal_or_none, positive_decimal

HTML_STAGE = "extract_from_html"
SKIPPED_PROPERTIES = frozenset(
    {
        "@context", "@id", "@type", "aggregateRating", "brand", "category", "description",
        "image", "name", "offers", "review", "reviews", "sku", "url", "hasVariant",
        "mpn", "gtin13", "gtin12", "gtin8", "productID", "itemCondition",
    }
)

logger = get_logger(__name__)


class WebsiteNormalizer(Normalizer):
    platform: ClassVar[SourcePlatform] = SourcePlatform.WEBSITE
    requires_llm: ClassVar[bool] = True

    def __init__(self, llm: LLMClient | None = None, model: str | None = None) -> None:
        super().__init__(llm)
        self.model = model

    async def normalize(self, record: dict[str, Any], context: SourceContext) -> Product:
        if record.get("_extraction") == "html":
            return await self._from_html(record, context)
        return normalize_jsonld_product(record, context)

    async def normalize_batch(
        self, records: list[dict[str, Any]], context: SourceContext
    ) -> list[Product]:
        products: list[Product] = []
        skipped = 0
        for record in records:
            try:
                products.append(await self.normalize(record, context))
            except NormalizationError as exc:
                skipped += 1
                log_event(
                    logger,
                    "skipped unusable record",
                    reason=exc.reason,
                    url=record.get("url"),
                )
        if skipped:
            log_event(
                logger, "normalization finished with skips", kept=len(products), skipped=skipped
            )
        return products

    async def _from_html(self, record: dict[str, Any], context: SourceContext) -> Product:
        if self.llm is None:
            raise NormalizationError(
                str(record.get("url") or ""), "html extraction needs an llm client"
            )
        url = str(record.get("url") or "")
        draft, _ = await self.llm.complete_json(
            prompt=render_prompt(
                "extract_from_html",
                record=record.get("html") or "",
                source_hint=f"Page URL: {url}" if url else "",
            ),
            response_model=ProductDraft,
            stage=HTML_STAGE,
            model=self.model,
        )
        return product_from_draft(draft, url, context)


def normalize_jsonld_product(record: dict[str, Any], context: SourceContext) -> Product:
    title = clean(record.get("name"))
    url = clean(record.get("url")) or context.base_url
    if not title:
        raise NormalizationError(str(url), "JSON-LD product has no name")

    offers = _offers(record.get("offers"))
    price, currency, compare_at, availability = _pricing(offers)
    variants = _variants(record, price, compare_at, availability)

    return Product(
        source_id=_source_id(record, url),
        source_platform=SourcePlatform.WEBSITE,
        source_url=url,
        handle=None,
        title=title,
        description=html_to_text(_text(record.get("description"))),
        brand=_brand(record.get("brand")),
        source_category=_text(record.get("category")),
        price=price,
        currency=currency,
        compare_at_price=compare_at,
        images=_images(record.get("image")),
        attributes=_attributes(record),
        variants=variants,
        tags=[],
        fetched_at=context.fetched_at,
        raw=record,
    )


def product_from_draft(draft: ProductDraft, url: str, context: SourceContext) -> Product:
    variants = [
        Variant(
            sku=variant.sku,
            title=variant.title,
            options=list(variant.options),
            price=decimal_or_none(variant.price),
        )
        for variant in draft.variants
    ] or [Variant()]
    attributes = [
        Attribute(
            name=attribute.name,
            value=attribute.value,
            unit=attribute.unit,
            origin=AttributeOrigin.LLM,
        )
        for attribute in draft.attributes
    ]
    return Product(
        source_id=url or draft.title,
        source_platform=SourcePlatform.WEBSITE,
        source_url=url or None,
        title=draft.title,
        description=draft.description,
        brand=draft.brand,
        price=next((v.price for v in variants if v.price is not None), None),
        images=[],
        attributes=attributes,
        variants=variants,
        tags=list(draft.tags),
        fetched_at=context.fetched_at,
        raw={"_extraction": "html", "url": url, "confidence": draft.confidence,
             "notes": draft.notes},
    )


def _source_id(record: dict[str, Any], url: str) -> str:
    for key in ("sku", "mpn", "productID", "gtin13", "@id"):
        value = clean(record.get(key))
        if value:
            return value
    return url


def _offers(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        if str(raw.get("@type", "")).lower() == "aggregateoffer":
            nested = raw.get("offers")
            if isinstance(nested, list):
                return [o for o in nested if isinstance(o, dict)] or [raw]
            return [raw]
        return [raw]
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    return []


def _pricing(
    offers: list[dict[str, Any]],
) -> tuple[Decimal | None, str | None, Decimal | None, bool | None]:
    prices: list[Decimal] = []
    currency: str | None = None
    availability: bool | None = None
    for offer in offers:
        for key in ("price", "lowPrice"):
            parsed = decimal_or_none(offer.get(key))
            if parsed is not None:
                prices.append(parsed)
                break
        code = clean(offer.get("priceCurrency"))
        if code and currency is None and len(code) == 3:
            currency = code.upper()
        state = clean(offer.get("availability"))
        if state is not None and availability is None:
            availability = "instock" in state.lower().replace("_", "")
    high = max(
        (d for offer in offers if (d := positive_decimal(offer.get("highPrice"))) is not None),
        default=None,
    )
    price = min(prices) if prices else None
    compare_at = high if high is not None and price is not None and high > price else None
    return price, currency, compare_at, availability


def _variants(
    record: dict[str, Any],
    price: Decimal | None,
    compare_at: Decimal | None,
    availability: bool | None,
) -> list[Variant]:
    raw = record.get("hasVariant")
    variants: list[Variant] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            item_price, _, _, item_available = _pricing(_offers(item.get("offers")))
            variants.append(
                Variant(
                    sku=clean(item.get("sku")),
                    title=clean(item.get("name")),
                    options=_variant_options(item),
                    price=item_price if item_price is not None else price,
                    available=item_available if item_available is not None else availability,
                )
            )
    if variants:
        return variants
    return [
        Variant(
            sku=clean(record.get("sku")),
            price=price,
            compare_at_price=compare_at,
            available=availability,
        )
    ]


def _variant_options(record: dict[str, Any]) -> list[VariantOption]:
    options: list[VariantOption] = []
    for key, label in (("size", "Size"), ("color", "Colour"), ("colour", "Colour")):
        value = _text(record.get(key))
        if value:
            options.append(VariantOption(name=label, value=value))
    return options


def _brand(raw: object) -> str | None:
    if isinstance(raw, dict):
        return clean(raw.get("name"))
    return clean(raw) if isinstance(raw, str) else None


def _images(raw: object) -> list[ProductImage]:
    urls: list[str] = []
    if isinstance(raw, str):
        urls = [raw]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                url = clean(item.get("url")) or clean(item.get("contentUrl"))
                if url:
                    urls.append(url)
    elif isinstance(raw, dict):
        url = clean(raw.get("url")) or clean(raw.get("contentUrl"))
        if url:
            urls.append(url)
    return [
        ProductImage(url=url, position=index)
        for index, url in enumerate(dict.fromkeys(urls), start=1)
        if url.startswith(("http://", "https://", "//"))
    ]


def _attributes(record: dict[str, Any]) -> list[Attribute]:
    attributes: list[Attribute] = []
    for item in record.get("additionalProperty") or []:
        if not isinstance(item, dict):
            continue
        name = clean(item.get("name"))
        value = _text(item.get("value"))
        if name and value:
            attributes.append(
                Attribute(
                    name=name,
                    value=value,
                    unit=clean(item.get("unitText")),
                    origin=AttributeOrigin.SOURCE,
                )
            )
    for key, value in record.items():
        if key in SKIPPED_PROPERTIES or key == "additionalProperty":
            continue
        text = _text(value)
        if text and len(text) <= 120:
            attributes.append(
                Attribute(name=_humanise(key), value=text, origin=AttributeOrigin.SOURCE)
            )
    return attributes


def _text(value: object) -> str | None:
    if isinstance(value, dict):
        return clean(value.get("name")) or clean(value.get("value"))
    if isinstance(value, list):
        parts = [_text(item) for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    return clean(value)


def _humanise(key: str) -> str:
    spaced = json.dumps(key)[1:-1].replace("_", " ")
    out: list[str] = []
    for index, char in enumerate(spaced):
        if char.isupper() and index and not spaced[index - 1].isupper():
            out.append(" ")
        out.append(char)
    return "".join(out).strip().capitalize()
