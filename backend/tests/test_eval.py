from __future__ import annotations

import csv
import pathlib
from decimal import Decimal

from app.eval.dataset import (
    LABEL_COLUMNS,
    EvalDataset,
    LabelRow,
    load_dataset,
    load_labels,
    load_products,
    merge_labels,
    save_labels,
    save_products,
)
from app.eval.metrics import mapping_report, retrieval_report
from app.eval.runner import write_results
from app.models.canonical import Product
from app.models.enums import SourcePlatform
from app.models.mapping import CategoryCandidate, MappedAttribute, MappedProduct
from app.pipeline.targets import load_target_specs


def product(source_id: str, title: str) -> Product:
    return Product(
        source_id=source_id,
        source_platform=SourcePlatform.SHOPIFY,
        title=title,
        price=Decimal("10.00"),
        raw={"id": source_id, "body_html": "<p>secret</p>"},
    )


def candidate(category_id: str, similarity: float = 0.5) -> CategoryCandidate:
    return CategoryCandidate(category_id=category_id, similarity=similarity, retriever="keyword")


def mapped(
    source_id: str,
    category_id: str | None,
    confidence: float,
    *,
    attributes: list[str] | None = None,
    needs_review: bool = False,
) -> MappedProduct:
    return MappedProduct(
        product=product(source_id, f"Product {source_id}"),
        target_category_id=category_id,
        category_confidence=confidence,
        category_reason="because",
        mapped_attributes=[
            MappedAttribute(target_key=key, value="x") for key in attributes or []
        ],
        needs_review=needs_review,
    )


def test_products_round_trip_without_the_raw_payload(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "products.jsonl"
    save_products([product("1", "Alpha"), product("2", "Beta")], path)
    assert "secret" not in path.read_text()
    restored = load_products(path)
    assert [p.source_id for p in restored] == ["1", "2"]
    assert restored[0].raw == {}


def test_missing_product_cache_loads_as_empty(tmp_path: pathlib.Path) -> None:
    assert load_products(tmp_path / "absent.jsonl") == []


def test_labels_round_trip_with_a_stable_header(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "labels.csv"
    save_labels([LabelRow(store="S", source_id="1", title="Alpha")], path)
    with path.open() as handle:
        assert next(csv.reader(handle)) == list(LABEL_COLUMNS)
    assert load_labels(path)[0].source_id == "1"


def test_merge_preserves_human_labels_over_fresh_suggestions() -> None:
    existing = [
        LabelRow(store="S", source_id="1", title="Alpha", expected_category_id="w_dresses"),
        LabelRow(store="S", source_id="2", title="Beta"),
    ]
    fresh = [
        LabelRow(store="S", source_id="1", title="Alpha", suggestion="w_skirts"),
        LabelRow(store="S", source_id="2", title="Beta", suggestion="w_jeans"),
    ]
    merged = {row.source_id: row for row in merge_labels(existing, fresh)}
    assert merged["1"].expected_category_id == "w_dresses"
    assert merged["1"].suggestion == "w_skirts"
    assert merged["2"].expected_category_id == ""


def test_merge_keeps_rows_that_are_no_longer_fetched() -> None:
    existing = [LabelRow(store="S", source_id="9", title="Gone", expected_category_id="w_jeans")]
    merged = merge_labels(existing, [LabelRow(store="S", source_id="1", title="Alpha")])
    assert {row.source_id for row in merged} == {"1", "9"}


def test_bootstrap_never_prefills_the_expected_category() -> None:
    row = LabelRow(store="S", source_id="1", title="Alpha", suggestion="w_dresses, w_skirts")
    assert row.expected_category_id == ""
    assert row.labelled is False


def test_dataset_counts_only_labelled_products() -> None:
    dataset = EvalDataset(
        products=[product("1", "Alpha"), product("2", "Beta")],
        labels=[
            LabelRow(store="S", source_id="1", title="Alpha", expected_category_id="w_dresses"),
            LabelRow(store="S", source_id="2", title="Beta"),
        ],
    )
    assert dataset.coverage() == (1, 2)
    assert [p.source_id for p in dataset.labelled_products()] == ["1"]
    assert dataset.expected() == {"1": "w_dresses"}


def test_retrieval_report_scores_recall_and_top1() -> None:
    shortlists = [
        (product("1", "A"), [candidate("w_jeans"), candidate("w_skirts")]),
        (product("2", "B"), [candidate("w_skirts"), candidate("w_dresses")]),
        (product("3", "C"), []),
    ]
    expected = {"1": "w_jeans", "2": "w_dresses", "3": "w_skirts"}
    report = retrieval_report(shortlists, expected, retriever="keyword", top_k=5)
    assert report.products == 3
    assert report.top1_accuracy == round(1 / 3, 4)
    assert report.recall_at_k == round(2 / 3, 4)
    assert report.no_candidate_rate == round(1 / 3, 4)
    assert report.mean_rank_of_correct == 1.5


def test_retrieval_report_ignores_unlabelled_products() -> None:
    shortlists = [(product("1", "A"), [candidate("w_jeans")]), (product("2", "B"), [])]
    report = retrieval_report(shortlists, {"1": "w_jeans"}, retriever="keyword", top_k=5)
    assert report.products == 1
    assert report.top1_accuracy == 1.0


def test_retrieval_report_handles_an_empty_label_set() -> None:
    report = retrieval_report([], {}, retriever="keyword", top_k=5)
    assert report.products == 0
    assert report.mean_rank_of_correct is None


def test_mapping_report_separates_confident_from_unsure() -> None:
    spec = load_target_specs()[0]
    results = [
        mapped("1", "w_knitwear", 0.95, attributes=["brand", "colour", "material", "size"]),
        mapped("2", "w_jeans", 0.9, attributes=["brand"]),
        mapped("3", "w_dresses", 0.4, needs_review=True),
        mapped("4", None, 0.0, needs_review=True),
    ]
    expected = {"1": "w_knitwear", "2": "w_skirts", "3": "w_dresses", "4": "w_jeans"}
    report = mapping_report(
        results,
        expected,
        spec,
        retriever="embedding",
        model="nemotron",
        total_tokens=400,
        elapsed_seconds=8.0,
    )
    assert report.products == 4
    assert report.accuracy == 0.5
    assert report.unmapped_rate == 0.25
    assert report.accuracy_when_confident == 0.5
    assert report.accuracy_when_unsure == 0.5
    assert report.tokens_per_product == 100.0
    assert report.seconds_per_product == 2.0


def test_mapping_report_measures_whether_review_catches_errors() -> None:
    spec = load_target_specs()[0]
    results = [
        mapped("1", "w_jeans", 0.95),
        mapped("2", "w_jeans", 0.3, needs_review=True),
    ]
    expected = {"1": "w_skirts", "2": "w_skirts"}
    report = mapping_report(
        results,
        expected,
        spec,
        retriever="embedding",
        model="nemotron",
        total_tokens=100,
        elapsed_seconds=1.0,
    )
    assert report.accuracy == 0.0
    assert report.review_catches_errors == 0.5


def test_mapping_report_attribute_completeness_counts_required_only() -> None:
    spec = load_target_specs()[0]
    results = [mapped("1", "w_knitwear", 0.9, attributes=["brand", "colour"])]
    report = mapping_report(
        results,
        {"1": "w_knitwear"},
        spec,
        retriever="embedding",
        model="nemotron",
        total_tokens=10,
        elapsed_seconds=1.0,
    )
    assert report.attribute_completeness == 0.5


def test_results_file_notes_when_mapping_was_skipped(tmp_path: pathlib.Path) -> None:
    spec = load_target_specs()[0]
    report = retrieval_report(
        [(product("1", "A"), [candidate("w_jeans")])],
        {"1": "w_jeans"},
        retriever="keyword",
        top_k=5,
    )
    path = tmp_path / "results.md"
    write_results(path, spec, [report], [])
    text = path.read_text()
    assert "# Evaluation results" in text
    assert "meridian_apparel" in text
    assert "needs a Nebius Token Factory key" in text
    assert "| keyword | 5 | 1 | 100.0% | 100.0% | 0.0% | 1.0 |" in text


def test_shipped_dataset_is_balanced_across_stores() -> None:
    dataset = load_dataset()
    assert len(dataset.products) == 104
    counts: dict[str, int] = {}
    for row in dataset.labels:
        counts[row.store] = counts.get(row.store, 0) + 1
    assert len(counts) == 4
    assert set(counts.values()) == {26}


def test_shipped_labels_are_not_prefilled() -> None:
    assert all(not row.labelled for row in load_dataset().labels)
