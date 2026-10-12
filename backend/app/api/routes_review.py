from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.schemas import (
    CategoryOption,
    CorrectionRequest,
    CorrectionSummary,
    JobRequest,
    JobSummary,
    ProductDetail,
    ProductSummary,
    ReviewStats,
    TargetSummary,
    UsageSummary,
)
from app.api.service import correction_examples, new_job, remap_unreviewed, run_job
from app.config import Settings, get_settings
from app.db import CorrectionRow, JobRow, MappedProductRow, Session, get_session, select, touch
from app.llm.usage import UsageTracker
from app.models.mapping import MappedProduct
from app.models.target import TargetSpec
from app.pipeline.load import get_loader
from app.pipeline.targets import find_target_spec, load_target_specs

router = APIRouter(prefix="/api")
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/targets", response_model=list[TargetSummary])
def list_targets() -> list[TargetSummary]:
    return [_target_summary(spec) for spec in load_target_specs()]


@router.get("/targets/{target_id}", response_model=TargetSummary)
def read_target(target_id: str) -> TargetSummary:
    return _target_summary(_spec_or_404(target_id))


@router.post("/jobs", response_model=JobSummary, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: JobRequest, session: SessionDep, settings: SettingsDep
) -> JobSummary:
    if not request.confirmed_ownership:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "confirmed_ownership must be true: only migrate a catalog you own or may migrate",
        )
    spec = _spec_or_404(request.target_id)
    if not settings.llm_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "NEBIUS_API_KEY is not set, so mapping cannot run",
        )
    job = new_job(session, request.source, request.platform or "", request.target_id)
    await run_job(
        session,
        job,
        spec,
        settings,
        limit=request.limit,
        retriever=request.retriever,
        tracker=UsageTracker(),
    )
    return _job_summary(job)


@router.get("/jobs", response_model=list[JobSummary])
def list_jobs(session: SessionDep) -> list[JobSummary]:
    rows = session.exec(select(JobRow)).all()
    ordered = sorted(rows, key=lambda row: row.created_at, reverse=True)
    return [_job_summary(row) for row in ordered]


@router.get("/jobs/{job_id}", response_model=JobSummary)
def read_job(job_id: str, session: SessionDep) -> JobSummary:
    return _job_summary(_job_or_404(session, job_id))


@router.get("/jobs/{job_id}/stats", response_model=ReviewStats)
def read_stats(job_id: str, session: SessionDep) -> ReviewStats:
    _job_or_404(session, job_id)
    rows = _products(session, job_id)
    corrections = len(
        session.exec(select(CorrectionRow).where(CorrectionRow.job_id == job_id)).all()
    )
    confidences = [row.category_confidence for row in rows]
    return ReviewStats(
        products=len(rows),
        needs_review=sum(1 for row in rows if row.needs_review and not row.reviewed),
        reviewed=sum(1 for row in rows if row.reviewed),
        unmapped=sum(1 for row in rows if row.target_category_id is None),
        mean_confidence=round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        corrections=corrections,
    )


@router.get("/jobs/{job_id}/products", response_model=list[ProductSummary])
def list_products(
    job_id: str,
    session: SessionDep,
    needs_review: bool | None = None,
    reviewed: bool | None = None,
) -> list[ProductSummary]:
    _job_or_404(session, job_id)
    rows = _products(session, job_id)
    if needs_review is not None:
        rows = [row for row in rows if row.needs_review is needs_review]
    if reviewed is not None:
        rows = [row for row in rows if row.reviewed is reviewed]
    return [_summary(row) for row in rows]


@router.get("/products/{product_id}", response_model=ProductDetail)
def read_product(product_id: str, session: SessionDep) -> ProductDetail:
    return _detail(_product_or_404(session, product_id))


@router.post("/products/{product_id}/correction", response_model=ProductDetail)
def submit_correction(
    product_id: str, request: CorrectionRequest, session: SessionDep
) -> ProductDetail:
    row = _product_or_404(session, product_id)
    job = _job_or_404(session, row.job_id)
    spec = _spec_or_404(job.target_id)
    if request.target_category_id and spec.category(request.target_category_id) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{request.target_category_id} is not a category of {spec.id}",
        )

    stored = MappedProduct.model_validate(row.payload)
    previous = stored.target_category_id
    if request.target_category_id:
        category = spec.category(request.target_category_id)
        stored.target_category_id = request.target_category_id
        stored.target_category_path = category.path if category else []
        stored.category_confidence = 1.0
        stored.category_reason = "set by a human reviewer"
    for key, value in request.attributes.items():
        _upsert_attribute(stored, key, value)
    stored.missing_attributes = [
        attribute.key
        for attribute in spec.required_attributes(stored.target_category_id)
        if attribute.key not in {a.target_key for a in stored.mapped_attributes}
    ]
    stored.needs_review = False

    row.target_category_id = stored.target_category_id
    row.category_confidence = stored.category_confidence
    row.needs_review = False
    row.reviewed = True
    row.payload = stored.model_dump(mode="json", exclude={"product": {"raw"}})
    session.add(row)
    session.add(
        CorrectionRow(
            job_id=job.id,
            product_id=row.id,
            source_id=row.source_id,
            title=row.title,
            from_category_id=previous,
            to_category_id=stored.target_category_id,
            attributes=dict(request.attributes),
            note=request.note,
        )
    )
    job.needs_review_count = sum(
        1 for item in _products(session, job.id) if item.needs_review and not item.reviewed
    )
    session.add(touch(job))
    session.commit()
    session.refresh(row)
    return _detail(row)


@router.get("/jobs/{job_id}/corrections", response_model=list[CorrectionSummary])
def list_corrections(job_id: str, session: SessionDep) -> list[CorrectionSummary]:
    _job_or_404(session, job_id)
    rows = session.exec(
        select(CorrectionRow)
        .where(CorrectionRow.job_id == job_id)
        .order_by(CorrectionRow.id.desc())  # type: ignore[union-attr]
    ).all()
    return [
        CorrectionSummary(
            product_id=row.product_id,
            source_id=row.source_id,
            title=row.title,
            from_category_id=row.from_category_id,
            to_category_id=row.to_category_id,
            note=row.note,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/jobs/{job_id}/relearn", response_model=ReviewStats)
async def relearn(
    job_id: str, session: SessionDep, settings: SettingsDep, retriever: str = "embedding"
) -> ReviewStats:
    job = _job_or_404(session, job_id)
    spec = _spec_or_404(job.target_id)
    if not settings.llm_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "NEBIUS_API_KEY is not set"
        )
    if not correction_examples(session, job_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no corrections yet, so there is nothing to learn from",
        )
    await remap_unreviewed(
        session, job, spec, settings, retriever=retriever, tracker=UsageTracker()
    )
    return read_stats(job_id, session)


@router.get("/jobs/{job_id}/export")
def export_job(job_id: str, session: SessionDep, fmt: str = "json") -> dict[str, str]:
    job = _job_or_404(session, job_id)
    spec = _spec_or_404(job.target_id)
    try:
        loader = get_loader(fmt)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    mapped = [MappedProduct.model_validate(row.payload) for row in _products(session, job.id)]
    return {
        "format": fmt,
        "filename": f"{job.id}{loader.extension}",
        "body": loader.dump(mapped, spec),
    }


@router.get("/usage", response_model=UsageSummary)
def read_usage() -> UsageSummary:
    return UsageSummary(calls=0, total_tokens=0, tokens_by_stage={}, tokens_by_model={})


def _spec_or_404(target_id: str) -> TargetSpec:
    spec = find_target_spec(target_id)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no target spec with id {target_id}")
    return spec


def _job_or_404(session: Session, job_id: str) -> JobRow:
    job = session.get(JobRow, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job with id {job_id}")
    return job


def _product_or_404(session: Session, product_id: str) -> MappedProductRow:
    row = session.get(MappedProductRow, product_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no product with id {product_id}")
    return row


def _products(session: Session, job_id: str) -> list[MappedProductRow]:
    return list(
        session.exec(
            select(MappedProductRow)
            .where(MappedProductRow.job_id == job_id)
            .order_by(MappedProductRow.category_confidence)  # type: ignore[arg-type]
        ).all()
    )


def _upsert_attribute(stored: MappedProduct, key: str, value: str) -> None:
    for attribute in stored.mapped_attributes:
        if attribute.target_key == key:
            attribute.value = value
            attribute.confidence = 1.0
            return
    from app.models.mapping import MappedAttribute

    stored.mapped_attributes.append(
        MappedAttribute(target_key=key, value=value, confidence=1.0)
    )


def _target_summary(spec: TargetSpec) -> TargetSummary:
    return TargetSummary(
        id=spec.id,
        name=spec.name,
        description=spec.description,
        languages=spec.languages,
        default_language=spec.default_language,
        confidence_threshold=spec.confidence_threshold,
        categories=[
            CategoryOption(
                id=category.id,
                name=category.name,
                path=category.path,
                required_attributes=[a.key for a in spec.required_attributes(category.id)],
            )
            for category in spec.categories
        ],
    )


def _job_summary(job: JobRow) -> JobSummary:
    return JobSummary(
        id=job.id,
        source=job.source,
        platform=job.platform,
        target_id=job.target_id,
        status=job.status,
        store_name=job.store_name,
        currency=job.currency,
        product_count=job.product_count,
        needs_review_count=job.needs_review_count,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _summary(row: MappedProductRow) -> ProductSummary:
    stored = MappedProduct.model_validate(row.payload)
    product = stored.product
    return ProductSummary(
        id=row.id,
        source_id=product.source_id,
        title=product.title,
        brand=product.brand,
        source_category=product.source_category,
        price=str(product.price) if product.price is not None else None,
        currency=product.currency,
        image_url=product.images[0].url if product.images else None,
        variant_count=len(product.variants),
        target_category_id=stored.target_category_id,
        target_category_path=stored.target_category_path,
        category_confidence=stored.category_confidence,
        category_reason=stored.category_reason,
        missing_attributes=stored.missing_attributes,
        needs_review=row.needs_review,
        reviewed=row.reviewed,
    )


def _detail(row: MappedProductRow) -> ProductDetail:
    stored = MappedProduct.model_validate(row.payload)
    product = stored.product
    base = _summary(row)
    variants: list[dict[str, Any]] = [
        {
            "sku": variant.sku,
            "title": variant.title,
            "options": [{"name": o.name, "value": o.value} for o in variant.options],
            "price": str(variant.price) if variant.price is not None else None,
            "available": variant.available,
            "stock": variant.stock,
        }
        for variant in product.variants
    ]
    return ProductDetail(
        **base.model_dump(),
        description=product.description,
        source_url=product.source_url,
        images=[image.url for image in product.images],
        tags=product.tags,
        attributes=stored.mapped_attributes,
        source_attributes=[
            {"name": a.name, "value": a.value, "unit": a.unit, "origin": a.origin.value}
            for a in product.attributes
        ],
        variants=variants,
        candidates=stored.category_candidates,
    )
