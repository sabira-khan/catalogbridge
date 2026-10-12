from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from app.models.canonical import CanonicalModel


class AttributeType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    ENUM = "enum"
    BOOLEAN = "boolean"


class TargetAttribute(CanonicalModel):
    key: str
    label: str
    type: AttributeType = AttributeType.TEXT
    required: bool = False
    unit: str | None = None
    allowed_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enum_needs_values(self) -> TargetAttribute:
        if self.type is AttributeType.ENUM and not self.allowed_values:
            raise ValueError(f"enum attribute {self.key} has no allowed_values")
        return self


class TargetCategory(CanonicalModel):
    id: str
    name: str
    path: list[str] = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    attributes: list[TargetAttribute] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return " > ".join(self.path)

    def embedding_text(self) -> str:
        parts = [self.label]
        if self.keywords:
            parts.append(", ".join(self.keywords))
        return ". ".join(parts)

    def keyword_terms(self) -> list[str]:
        return [term.lower() for term in [self.name, *self.keywords]]


class TargetSpec(CanonicalModel):
    id: str
    name: str
    description: str | None = None
    languages: list[str] = Field(min_length=1)
    default_language: str
    currency: str | None = None
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    global_attributes: list[TargetAttribute] = Field(default_factory=list)
    categories: list[TargetCategory] = Field(min_length=1)

    @model_validator(mode="after")
    def check_consistency(self) -> TargetSpec:
        ids = [category.id for category in self.categories]
        duplicates = {name for name in ids if ids.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate category ids: {sorted(duplicates)}")
        if self.default_language not in self.languages:
            raise ValueError(
                f"default_language {self.default_language} is not in languages {self.languages}"
            )
        return self

    def category(self, category_id: str) -> TargetCategory | None:
        return self.index().get(category_id)

    def index(self) -> dict[str, TargetCategory]:
        return {category.id: category for category in self.categories}

    def required_attributes(self, category_id: str | None) -> list[TargetAttribute]:
        attributes = [a for a in self.global_attributes if a.required]
        category = self.category(category_id) if category_id else None
        if category:
            attributes.extend(a for a in category.attributes if a.required)
        return attributes

    def attributes_for(self, category_id: str | None) -> list[TargetAttribute]:
        attributes = list(self.global_attributes)
        category = self.category(category_id) if category_id else None
        if category:
            attributes.extend(category.attributes)
        return attributes

    def needs_review(self, confidence: float) -> bool:
        return confidence < self.confidence_threshold
