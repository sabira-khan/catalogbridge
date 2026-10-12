from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.mapping import CategoryCandidate, MappedAttribute


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobRequest(ApiModel):
    source: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    platform: str | None = None
    limit: int | None = Field(default=None, ge=1, le=5000)
    retriever: str = "embedding"
    confirmed_ownership: bool = False


class JobSummary(ApiModel):
    id: str
    source: str
    platform: str
    target_id: str
    status: str
    store_name: str | None
    currency: str | None
    product_count: int
    needs_review_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class ProductSummary(ApiModel):
    id: str
    job_id: str
    source_id: str
    title: str
    brand: str | None
    source_category: str | None
    price: str | None
    currency: str | None
    image_url: str | None
    variant_count: int
    target_category_id: str | None
    target_category_path: list[str]
    category_confidence: float
    category_reason: str
    missing_attributes: list[str]
    needs_review: bool
    reviewed: bool


class ProductDetail(ProductSummary):
    description: str | None
    source_url: str | None
    images: list[str]
    tags: list[str]
    attributes: list[MappedAttribute]
    source_attributes: list[dict[str, str | None]]
    variants: list[dict[str, object]]
    candidates: list[CategoryCandidate]


class CorrectionRequest(ApiModel):
    target_category_id: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


class CorrectionSummary(ApiModel):
    product_id: str
    source_id: str
    title: str
    from_category_id: str | None
    to_category_id: str | None
    note: str | None
    created_at: datetime


class CategoryOption(ApiModel):
    id: str
    name: str
    path: list[str]
    required_attributes: list[str]


class TargetSummary(ApiModel):
    id: str
    name: str
    description: str | None
    languages: list[str]
    default_language: str
    confidence_threshold: float
    categories: list[CategoryOption]


class ReviewStats(ApiModel):
    products: int
    needs_review: int
    reviewed: int
    unmapped: int
    mean_confidence: float
    corrections: int


class UsageSummary(ApiModel):
    calls: int
    total_tokens: int
    tokens_by_stage: dict[str, int]
    tokens_by_model: dict[str, int]
