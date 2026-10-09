from __future__ import annotations

from pydantic import Field

from app.models.canonical import Attribute, CanonicalModel, Product


class CategoryCandidate(CanonicalModel):
    category_id: str
    path: list[str] = Field(default_factory=list)
    similarity: float = Field(ge=0.0, le=1.0)


class MappedAttribute(CanonicalModel):
    target_key: str
    value: str
    unit: str | None = None
    source_attribute: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Translation(CanonicalModel):
    title: str
    description: str | None = None
    attributes: list[Attribute] = Field(default_factory=list)


class MappedProduct(CanonicalModel):
    product: Product
    target_category_id: str | None = None
    target_category_path: list[str] = Field(default_factory=list)
    category_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    category_reason: str = ""
    category_candidates: list[CategoryCandidate] = Field(default_factory=list)
    mapped_attributes: list[MappedAttribute] = Field(default_factory=list)
    translations: dict[str, Translation] = Field(default_factory=dict)
    needs_review: bool = True
