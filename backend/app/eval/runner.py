from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.connectors.base import SourceContext
from app.connectors.registry import build_client, get_connector
from app.eval.dataset import (
    LabelRow,
    load_dataset,
    load_labels,
    merge_labels,
    save_labels,
    save_products,
)
from app.eval.metrics import MappingReport, RetrievalReport, mapping_report, retrieval_report
from app.llm.client import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.models.canonical import Product
from app.models.enums import SourcePlatform
from app.models.mapping import CategoryCandidate
from app.models.target import TargetSpec
from app.pipeline.candidates import CandidateSource, EmbeddingCandidates, KeywordCandidates
from app.pipeline.map import CategoryMapper
from app.pipeline.registry import get_normalizer

SUGGESTION_COUNT = 3


async def collect_products(
    stores: list[str], settings: Settings, per_store: int
) -> list[tuple[str, Product]]:
    collected: list[tuple[str, Product]] = []
    async with build_client(settings) as client:
        for store in stores:
            connector = get_connector(SourcePlatform.SHOPIFY, store, settings, client)
            context = await connector.discover()
            records = []
            async for record in connector.iter_records():
                records.append(record)
                if len(records) >= per_store:
                    break
            products = await get_normalizer(SourcePlatform.SHOPIFY).normalize_batch(
                records, context
            )
            collected.extend((_store_name(context, store), product) for product in products)
    return collected


async def bootstrap_labels(
    pairs: list[tuple[str, Product]], spec: TargetSpec, products_path: Path, labels_path: Path
) -> list[LabelRow]:
    source = KeywordCandidates()
    rows: list[LabelRow] = []
    for store, product in pairs:
        candidates = await source.candidates(product, spec, SUGGESTION_COUNT)
        rows.append(
            LabelRow(
                store=store,
                source_id=product.source_id,
                title=product.title,
                source_category=product.source_category or "",
                expected_category_id="",
                suggestion=", ".join(c.category_id for c in candidates),
            )
        )
    merged = merge_labels(load_labels(labels_path), rows)
    save_products([product for _, product in pairs], products_path)
    save_labels(merged, labels_path)
    return merged


def build_candidate_source(settings: Settings, retriever: str) -> CandidateSource:
    if retriever == KeywordCandidates.name:
        return KeywordCandidates()
    return EmbeddingCandidates(EmbeddingClient(settings))


async def run_retrieval(
    spec: TargetSpec, settings: Settings, retriever: str, top_k: int
) -> RetrievalReport:
    dataset = load_dataset()
    source = build_candidate_source(settings, retriever)
    shortlists: list[tuple[Product, list[CategoryCandidate]]] = []
    for product in dataset.labelled_products():
        shortlists.append((product, await source.candidates(product, spec, top_k)))
    return retrieval_report(
        shortlists, dataset.expected(), retriever=retriever, top_k=top_k
    )


async def run_mapping(
    spec: TargetSpec, settings: Settings, retriever: str, top_k: int
) -> MappingReport:
    dataset = load_dataset()
    llm = LLMClient(settings)
    mapper = CategoryMapper(
        spec, llm, build_candidate_source(settings, retriever), top_k=top_k
    )
    started = time.monotonic()
    mapped = await mapper.map_products(dataset.labelled_products())
    elapsed = time.monotonic() - started
    return mapping_report(
        mapped,
        dataset.expected(),
        spec,
        retriever=retriever,
        model=settings.llm_model,
        total_tokens=llm.tracker.totals().total_tokens,
        elapsed_seconds=elapsed,
    )


def write_results(
    path: Path,
    spec: TargetSpec,
    retrieval: list[RetrievalReport],
    mapping: list[MappingReport],
) -> None:
    lines = [
        "# Evaluation results",
        "",
        f"Target: **{spec.name}** (`{spec.id}`), {len(spec.categories)} categories",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Retrieval",
        "",
        "Does the correct category reach the shortlist at all? This is the ceiling on",
        "everything the model can do afterwards.",
        "",
        "| Retriever | k | Products | Recall@k | Top-1 | No candidate | Mean rank |",
        "|---|---|---|---|---|---|---|",
    ]
    for report in retrieval:
        rank = "-" if report.mean_rank_of_correct is None else f"{report.mean_rank_of_correct}"
        lines.append(
            f"| {report.retriever} | {report.top_k} | {report.products} "
            f"| {_pct(report.recall_at_k)} | {_pct(report.top1_accuracy)} "
            f"| {_pct(report.no_candidate_rate)} | {rank} |"
        )

    lines += [
        "",
        "## Mapping",
        "",
        "Top-1 is the baseline to beat: it is what you get by picking the highest",
        "scoring retrieval result with no model at all.",
        "",
        "| Retriever | Model | Products | Accuracy | Unmapped | Attributes | Review rate |",
        "|---|---|---|---|---|---|---|",
    ]
    if mapping:
        for row in mapping:
            lines.append(
                f"| {row.retriever} | `{row.model}` | {row.products} "
                f"| {_pct(row.accuracy)} | {_pct(row.unmapped_rate)} "
                f"| {_pct(row.attribute_completeness)} | {_pct(row.needs_review_rate)} |"
            )
        lines += [
            "",
            "### Is the confidence score trustworthy?",
            "",
            "If accuracy when confident is high and accuracy when unsure is low, the",
            "threshold is doing real work and a reviewer's time goes where it matters.",
            "",
            "| Model | Mean confidence | Accuracy when confident | Accuracy when unsure "
            "| Errors caught by review | Tokens/product | Seconds/product |",
            "|---|---|---|---|---|---|---|",
        ]
        for entry in mapping:
            lines.append(
                f"| `{entry.model}` | {entry.mean_confidence:.2f} "
                f"| {_opt_pct(entry.accuracy_when_confident)} "
                f"| {_opt_pct(entry.accuracy_when_unsure)} "
                f"| {_opt_pct(entry.review_catches_errors)} "
                f"| {entry.tokens_per_product} | {entry.seconds_per_product} |"
            )
    else:
        lines.append("| _not run_ | | | | | | |")
        lines += [
            "",
            "Mapping needs a Nebius Token Factory key. Retrieval above ran without one.",
        ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _store_name(context: SourceContext, fallback: str) -> str:
    return context.store_name or context.base_url or fallback


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _opt_pct(value: float | None) -> str:
    return "-" if value is None else _pct(value)
