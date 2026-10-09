from __future__ import annotations

from pydantic import Field

from app.models.canonical import Attribute, CanonicalModel, VariantOption


class VariantDraft(CanonicalModel):
    sku: str | None = None
    title: str | None = None
    options: list[VariantOption] = Field(default_factory=list)
    price: str | None = None


class ProductDraft(CanonicalModel):
    title: str
    description: str | None = None
    brand: str | None = None
    attributes: list[Attribute] = Field(default_factory=list)
    variants: list[VariantDraft] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = None
