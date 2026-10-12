from __future__ import annotations

import csv
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.models.canonical import Product

EVAL_DIR = Path(__file__).resolve().parents[3] / "eval"
DATASET_DIR = EVAL_DIR / "dataset"
PRODUCTS_FILE = DATASET_DIR / "products.jsonl"
LABELS_FILE = EVAL_DIR / "labels.csv"
RESULTS_FILE = EVAL_DIR / "results.md"

LABEL_COLUMNS = (
    "store",
    "source_id",
    "title",
    "source_category",
    "expected_category_id",
    "suggestion",
    "notes",
)


class LabelRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    store: str
    source_id: str
    title: str
    source_category: str = ""
    expected_category_id: str = ""
    suggestion: str = ""
    notes: str = ""

    @property
    def labelled(self) -> bool:
        return bool(self.expected_category_id)


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[Product] = Field(default_factory=list)
    labels: list[LabelRow] = Field(default_factory=list)

    def expected(self) -> dict[str, str]:
        return {row.source_id: row.expected_category_id for row in self.labels if row.labelled}

    def labelled_products(self) -> list[Product]:
        expected = self.expected()
        return [p for p in self.products if p.source_id in expected]

    def coverage(self) -> tuple[int, int]:
        return len(self.expected()), len(self.labels)


def save_products(products: list[Product], path: Path = PRODUCTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(product.model_dump(mode="json", exclude={"raw"}), ensure_ascii=False)
        for product in products
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_products(path: Path = PRODUCTS_FILE) -> list[Product]:
    if not path.is_file():
        return []
    products: list[Product] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            products.append(Product.model_validate(json.loads(line)))
    return products


def save_labels(rows: list[LabelRow], path: Path = LABELS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LABEL_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.model_dump())


def load_labels(path: Path = LABELS_FILE) -> list[LabelRow]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [LabelRow.model_validate(raw) for raw in csv.DictReader(handle)]


def load_dataset(
    products_path: Path = PRODUCTS_FILE, labels_path: Path = LABELS_FILE
) -> EvalDataset:
    return EvalDataset(
        products=load_products(products_path), labels=load_labels(labels_path)
    )


def merge_labels(existing: list[LabelRow], fresh: list[LabelRow]) -> list[LabelRow]:
    by_id = {row.source_id: row for row in fresh}
    for row in existing:
        if row.labelled and row.source_id in by_id:
            by_id[row.source_id].expected_category_id = row.expected_category_id
            by_id[row.source_id].notes = row.notes or by_id[row.source_id].notes
        elif row.source_id not in by_id:
            by_id[row.source_id] = row
    return sorted(by_id.values(), key=lambda row: (row.store, row.title))
