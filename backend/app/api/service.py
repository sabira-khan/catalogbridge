from __future__ import annotations

import uuid
from typing import Any

from app.config import Settings
from app.connectors.detect import detect_source
from app.connectors.registry import build_client, get_connector
from app.connectors.spreadsheet import SpreadsheetConnector
from app.db import CorrectionRow, JobRow, MappedProductRow, Session, select, touch
from app.errors import MissingAPIKeyError
from app.llm.client import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.llm.usage import UsageTracker
from app.models.canonical import Product
from app.models.enums import SourcePlatform
from app.models.mapping import CorrectionExample, MappedProduct
from app.models.target import TargetSpec
from app.pipeline.candidates import EmbeddingCandidates, KeywordCandidates
from app.pipeline.map import CategoryMapper
from app.pipeline.registry import get_normalizer


def new_job(session: Session, source: str, platform: str, target_id: str) -> JobRow:
    job = JobRow(
        id=uuid.uuid4().hex, source=source, platform=platform, target_id=target_id, status="running"
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


async def run_job(
    session: Session,
    job: JobRow,
    spec: TargetSpec,
    settings: Settings,
    *,
    limit: int | None,
    retriever: str,
    tracker: UsageTracker,
) -> JobRow:
    try:
        products, platform, store_name, currency = await _extract(
            source=job.source, platform=job.platform, settings=settings, limit=limit
        )
        job.platform = platform.value
        job.store_name = store_name
        job.currency = currency
        mapped = await _map(products, spec, settings, retriever, tracker)
        _store(session, job, mapped)
        job.status = "ready"
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
    session.add(touch(job))
    session.commit()
    session.refresh(job)
    return job


async def _extract(
    *, source: str, platform: str, settings: Settings, limit: int | None
) -> tuple[list[Product], SourcePlatform, str | None, str | None]:
    async with build_client(settings) as client:
        resolved = (
            SourcePlatform(platform)
            if platform
            else await detect_source(source, settings, client)
        )
        connector = get_connector(resolved, source, settings, client)
        context = await connector.discover()
        records: list[dict[str, Any]] = []
        async for record in connector.iter_records():
            records.append(record)
            if limit is not None and len(records) >= limit:
                break
        llm = LLMClient(settings) if settings.llm_configured else None
        normalizer = get_normalizer(resolved, llm)
        if isinstance(connector, SpreadsheetConnector) and normalizer.requires_llm:
            if llm is None:
                raise MissingAPIKeyError
            await normalizer.learn_headers(connector.headers, connector.sample())  # type: ignore[attr-defined]
        products = await normalizer.normalize_batch(records, context)
        return products, resolved, context.store_name, context.currency


async def _map(
    products: list[Product],
    spec: TargetSpec,
    settings: Settings,
    retriever: str,
    tracker: UsageTracker,
) -> list[MappedProduct]:
    llm = LLMClient(settings, tracker=tracker)
    source = (
        KeywordCandidates()
        if retriever == KeywordCandidates.name
        else EmbeddingCandidates(EmbeddingClient(settings, tracker=tracker))
    )
    mapper = CategoryMapper(spec, llm, source)
    return await mapper.map_products(products)


def _store(session: Session, job: JobRow, mapped: list[MappedProduct]) -> None:
    for item in mapped:
        session.add(
            MappedProductRow(
                id=uuid.uuid4().hex,
                job_id=job.id,
                source_id=item.product.source_id,
                title=item.product.title,
                target_category_id=item.target_category_id,
                category_confidence=item.category_confidence,
                needs_review=item.needs_review,
                payload=item.model_dump(mode="json", exclude={"product": {"raw"}}),
            )
        )
    job.product_count = len(mapped)
    job.needs_review_count = sum(1 for item in mapped if item.needs_review)


def correction_examples(session: Session, job_id: str, limit: int = 12) -> list[CorrectionExample]:
    rows = session.exec(
        select(CorrectionRow)
        .where(CorrectionRow.job_id == job_id)
        .order_by(CorrectionRow.id.desc())  # type: ignore[union-attr]
        .limit(limit)
    ).all()
    examples: list[CorrectionExample] = []
    for row in rows:
        if not row.to_category_id:
            continue
        examples.append(
            CorrectionExample(
                title=row.title,
                category_id=row.to_category_id,
                category_path=[],
            )
        )
    return examples


async def remap_unreviewed(
    session: Session,
    job: JobRow,
    spec: TargetSpec,
    settings: Settings,
    *,
    retriever: str,
    tracker: UsageTracker,
) -> int:
    examples = correction_examples(session, job.id)
    if not examples:
        return 0
    rows = session.exec(
        select(MappedProductRow).where(
            MappedProductRow.job_id == job.id,
            MappedProductRow.reviewed == False,  # noqa: E712
            MappedProductRow.needs_review == True,  # noqa: E712
        )
    ).all()
    if not rows:
        return 0

    llm = LLMClient(settings, tracker=tracker)
    source = (
        KeywordCandidates()
        if retriever == KeywordCandidates.name
        else EmbeddingCandidates(EmbeddingClient(settings, tracker=tracker))
    )
    mapper = CategoryMapper(spec, llm, source)
    updated = 0
    for row in rows:
        stored = MappedProduct.model_validate(row.payload)
        remapped = await mapper.map_product(stored.product, examples)
        row.target_category_id = remapped.target_category_id
        row.category_confidence = remapped.category_confidence
        row.needs_review = remapped.needs_review
        row.payload = remapped.model_dump(mode="json", exclude={"product": {"raw"}})
        session.add(row)
        updated += 1

    job.needs_review_count = sum(
        1
        for candidate in session.exec(
            select(MappedProductRow).where(MappedProductRow.job_id == job.id)
        ).all()
        if candidate.needs_review and not candidate.reviewed
    )
    session.add(touch(job))
    session.commit()
    return updated
