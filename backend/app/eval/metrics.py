from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.canonical import Product
from app.models.mapping import CategoryCandidate, MappedProduct
from app.models.target import TargetSpec

HIGH_CONFIDENCE = 0.8


class RetrievalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retriever: str
    top_k: int
    products: int
    recall_at_k: float
    top1_accuracy: float
    no_candidate_rate: float
    mean_rank_of_correct: float | None


class MappingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retriever: str
    model: str
    products: int
    accuracy: float
    unmapped_rate: float
    attribute_completeness: float
    needs_review_rate: float
    mean_confidence: float
    accuracy_when_confident: float | None
    accuracy_when_unsure: float | None
    review_catches_errors: float | None
    tokens_per_product: float
    seconds_per_product: float


def retrieval_report(
    shortlists: list[tuple[Product, list[CategoryCandidate]]],
    expected: dict[str, str],
    *,
    retriever: str,
    top_k: int,
) -> RetrievalReport:
    scored = [(p, c) for p, c in shortlists if p.source_id in expected]
    total = len(scored)
    if not total:
        return RetrievalReport(
            retriever=retriever,
            top_k=top_k,
            products=0,
            recall_at_k=0.0,
            top1_accuracy=0.0,
            no_candidate_rate=0.0,
            mean_rank_of_correct=None,
        )

    hits = 0
    top1 = 0
    empty = 0
    ranks: list[int] = []
    for product, candidates in scored:
        want = expected[product.source_id]
        ids = [candidate.category_id for candidate in candidates]
        if not ids:
            empty += 1
            continue
        if ids[0] == want:
            top1 += 1
        if want in ids:
            hits += 1
            ranks.append(ids.index(want) + 1)

    return RetrievalReport(
        retriever=retriever,
        top_k=top_k,
        products=total,
        recall_at_k=_ratio(hits, total),
        top1_accuracy=_ratio(top1, total),
        no_candidate_rate=_ratio(empty, total),
        mean_rank_of_correct=round(sum(ranks) / len(ranks), 2) if ranks else None,
    )


def mapping_report(
    mapped: list[MappedProduct],
    expected: dict[str, str],
    spec: TargetSpec,
    *,
    retriever: str,
    model: str,
    total_tokens: int,
    elapsed_seconds: float,
) -> MappingReport:
    scored = [m for m in mapped if m.product.source_id in expected]
    total = len(scored)
    if not total:
        return MappingReport(
            retriever=retriever,
            model=model,
            products=0,
            accuracy=0.0,
            unmapped_rate=0.0,
            attribute_completeness=0.0,
            needs_review_rate=0.0,
            mean_confidence=0.0,
            accuracy_when_confident=None,
            accuracy_when_unsure=None,
            review_catches_errors=None,
            tokens_per_product=0.0,
            seconds_per_product=0.0,
        )

    correct = [m for m in scored if m.target_category_id == expected[m.product.source_id]]
    confident = [m for m in scored if m.category_confidence >= HIGH_CONFIDENCE]
    unsure = [m for m in scored if m.category_confidence < HIGH_CONFIDENCE]
    wrong = [m for m in scored if m.target_category_id != expected[m.product.source_id]]
    wrong_and_flagged = [m for m in wrong if m.needs_review]

    return MappingReport(
        retriever=retriever,
        model=model,
        products=total,
        accuracy=_ratio(len(correct), total),
        unmapped_rate=_ratio(sum(1 for m in scored if m.target_category_id is None), total),
        attribute_completeness=_attribute_completeness(scored, spec),
        needs_review_rate=_ratio(sum(1 for m in scored if m.needs_review), total),
        mean_confidence=round(sum(m.category_confidence for m in scored) / total, 4),
        accuracy_when_confident=_subset_accuracy(confident, expected),
        accuracy_when_unsure=_subset_accuracy(unsure, expected),
        review_catches_errors=_ratio(len(wrong_and_flagged), len(wrong)) if wrong else None,
        tokens_per_product=round(total_tokens / total, 1),
        seconds_per_product=round(elapsed_seconds / total, 3),
    )


def _attribute_completeness(mapped: list[MappedProduct], spec: TargetSpec) -> float:
    required = 0
    supplied = 0
    for item in mapped:
        wanted = spec.required_attributes(item.target_category_id)
        if not wanted:
            continue
        required += len(wanted)
        have = {attribute.target_key for attribute in item.mapped_attributes}
        supplied += sum(1 for attribute in wanted if attribute.key in have)
    return _ratio(supplied, required)


def _subset_accuracy(subset: list[MappedProduct], expected: dict[str, str]) -> float | None:
    if not subset:
        return None
    hits = sum(1 for m in subset if m.target_category_id == expected[m.product.source_id])
    return _ratio(hits, len(subset))


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0
