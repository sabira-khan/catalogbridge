from __future__ import annotations

import csv
import io
import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.errors import UnsupportedTargetFormatError
from app.models.canonical import Variant
from app.models.mapping import MappedProduct
from app.models.target import TargetSpec

PRODUCT_COLUMNS = (
    "handle",
    "title",
    "description",
    "brand",
    "source_url",
    "target_category_id",
    "target_category_path",
    "category_confidence",
    "category_reason",
    "needs_review",
)
VARIANT_COLUMNS = (
    "sku",
    "variant_title",
    "option1_name",
    "option1_value",
    "option2_name",
    "option2_value",
    "option3_name",
    "option3_value",
    "price",
    "compare_at_price",
    "currency",
    "available",
    "image_url",
)
EXTRA_ATTRIBUTES_COLUMN = "extra_attributes"
MAX_OPTIONS = 3


class Loader(ABC):
    format: ClassVar[str]
    extension: ClassVar[str]

    @abstractmethod
    def dump(self, products: list[MappedProduct], spec: TargetSpec) -> str: ...


class JsonLoader(Loader):
    format = "json"
    extension = ".json"

    def dump(self, products: list[MappedProduct], spec: TargetSpec) -> str:
        payload = {
            "target": {
                "id": spec.id,
                "name": spec.name,
                "default_language": spec.default_language,
                "confidence_threshold": spec.confidence_threshold,
            },
            "summary": _summary(products, spec),
            "products": [p.model_dump(mode="json", exclude={"product": {"raw"}}) for p in products],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


class CsvLoader(Loader):
    format = "csv"
    extension = ".csv"

    def dump(self, products: list[MappedProduct], spec: TargetSpec) -> str:
        attribute_keys = [a.key for a in spec.global_attributes]
        header = [
            *PRODUCT_COLUMNS,
            *VARIANT_COLUMNS,
            *attribute_keys,
            EXTRA_ATTRIBUTES_COLUMN,
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for mapped in products:
            base = _product_row(mapped)
            attributes = _attribute_columns(mapped, attribute_keys)
            variants = mapped.product.variants or [Variant()]
            for variant in variants:
                writer.writerow({**base, **_variant_row(variant, mapped), **attributes})
        return buffer.getvalue()


LOADERS: dict[str, type[Loader]] = {
    JsonLoader.format: JsonLoader,
    CsvLoader.format: CsvLoader,
}


def get_loader(name: str) -> Loader:
    try:
        loader_class = LOADERS[name]
    except KeyError as exc:
        raise UnsupportedTargetFormatError(name) from exc
    return loader_class()


def _summary(products: list[MappedProduct], spec: TargetSpec) -> dict[str, Any]:
    flagged = [p for p in products if p.needs_review]
    confidences = [p.category_confidence for p in products]
    return {
        "products": len(products),
        "needs_review": len(flagged),
        "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "unmapped": sum(1 for p in products if p.target_category_id is None),
        "confidence_threshold": spec.confidence_threshold,
    }


def _product_row(mapped: MappedProduct) -> dict[str, str]:
    product = mapped.product
    return {
        "handle": product.handle or "",
        "title": product.title,
        "description": product.description or "",
        "brand": product.brand or "",
        "source_url": product.source_url or "",
        "target_category_id": mapped.target_category_id or "",
        "target_category_path": " > ".join(mapped.target_category_path),
        "category_confidence": f"{mapped.category_confidence:.2f}",
        "category_reason": mapped.category_reason,
        "needs_review": "yes" if mapped.needs_review else "no",
    }


def _variant_row(variant: Variant, mapped: MappedProduct) -> dict[str, str]:
    row = {
        "sku": variant.sku or "",
        "variant_title": variant.title or "",
        "price": str(variant.price) if variant.price is not None else "",
        "compare_at_price": (
            str(variant.compare_at_price) if variant.compare_at_price is not None else ""
        ),
        "currency": mapped.product.currency or "",
        "available": "" if variant.available is None else ("yes" if variant.available else "no"),
        "image_url": variant.image.url if variant.image else "",
    }
    for index in range(MAX_OPTIONS):
        option = variant.options[index] if index < len(variant.options) else None
        row[f"option{index + 1}_name"] = option.name if option else ""
        row[f"option{index + 1}_value"] = option.value if option else ""
    return row


def _attribute_columns(mapped: MappedProduct, keys: list[str]) -> dict[str, str]:
    known = set(keys)
    columns = dict.fromkeys(keys, "")
    extras: list[str] = []
    for attribute in mapped.mapped_attributes:
        value = attribute.value
        if attribute.unit:
            value = f"{value} {attribute.unit}"
        if attribute.target_key in known:
            columns[attribute.target_key] = value
        else:
            extras.append(f"{attribute.target_key}={value}")
    columns[EXTRA_ATTRIBUTES_COLUMN] = "; ".join(extras)
    return columns
