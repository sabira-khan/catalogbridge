from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import AttributeOrigin, SourcePlatform

LLM_EXCLUDED_FIELDS = {"raw", "source_id", "source_platform", "fetched_at"}


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductImage(CanonicalModel):
    url: str
    alt: str | None = None
    position: int | None = None
    width: int | None = None
    height: int | None = None

    @field_validator("url")
    @classmethod
    def absolutize_url(cls, value: str) -> str:
        return f"https:{value}" if value.startswith("//") else value


class Attribute(CanonicalModel):
    name: str
    value: str
    unit: str | None = None
    origin: AttributeOrigin = AttributeOrigin.SOURCE


class VariantOption(CanonicalModel):
    name: str
    value: str


class Variant(CanonicalModel):
    source_id: str | None = None
    sku: str | None = None
    title: str | None = None
    options: list[VariantOption] = Field(default_factory=list)
    price: Decimal | None = None
    compare_at_price: Decimal | None = None
    stock: int | None = None
    available: bool | None = None
    image: ProductImage | None = None


class Product(CanonicalModel):
    source_id: str
    source_platform: SourcePlatform
    source_url: str | None = None
    handle: str | None = None
    title: str
    description: str | None = None
    brand: str | None = None
    source_category: str | None = None
    price: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    compare_at_price: Decimal | None = None
    images: list[ProductImage] = Field(default_factory=list)
    attributes: list[Attribute] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    fetched_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def derive_price_from_variants(self) -> Product:
        if self.price is None:
            prices = [v.price for v in self.variants if v.price is not None]
            if prices:
                self.price = min(prices)
        return self

    def for_llm(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude=LLM_EXCLUDED_FIELDS, exclude_none=True)
