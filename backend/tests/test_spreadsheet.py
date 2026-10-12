from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from app.connectors.base import SourceContext
from app.connectors.registry import build_client, get_connector
from app.connectors.spreadsheet import SpreadsheetConnector
from app.errors import NormalizationError, UnsupportedSourceError
from app.llm.client import LLMClient
from app.models.enums import AttributeOrigin, SourcePlatform
from app.models.llm_io import ColumnMapping, HeaderMapping
from app.pipeline.spreadsheet import SpreadsheetNormalizer, build_product

from tests.conftest import SettingsFactory

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CSV_PATH = FIXTURES / "vendor_catalog.csv"
TSV_PATH = FIXTURES / "tab_catalog.tsv"
FETCHED_AT = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

VENDOR_MAPPING = HeaderMapping(
    mappings=[
        ColumnMapping(column="Internal ID", canonical_field="ignore", confidence=0.9),
        ColumnMapping(column="Product Name", canonical_field="title", confidence=0.98),
        ColumnMapping(column="Long Description", canonical_field="description", confidence=0.95),
        ColumnMapping(column="Maker", canonical_field="brand", confidence=0.9),
        ColumnMapping(column="Our Category", canonical_field="source_category", confidence=0.9),
        ColumnMapping(column="Retail (GBP)", canonical_field="price", confidence=0.95),
        ColumnMapping(column="Was Price", canonical_field="compare_at_price", confidence=0.85),
        ColumnMapping(column="Stock Qty", canonical_field="stock", confidence=0.9),
        ColumnMapping(column="Variant", canonical_field="option_name", confidence=0.8),
        ColumnMapping(column="Variant Val", canonical_field="option_value", confidence=0.8),
        ColumnMapping(
            column="Net weight (g)",
            canonical_field="attribute",
            attribute_name="Net weight",
            unit="g",
            confidence=0.9,
        ),
        ColumnMapping(
            column="Fabric", canonical_field="attribute", attribute_name="Fabric", confidence=0.9
        ),
        ColumnMapping(column="Photo URL", canonical_field="image", confidence=0.95),
        ColumnMapping(column="Keywords", canonical_field="tags", confidence=0.9),
        ColumnMapping(column="Cost Price", canonical_field="ignore", confidence=0.95),
        ColumnMapping(column="Warehouse Bin", canonical_field="ignore", confidence=0.95),
    ]
)


@dataclass
class FakeUsage:
    prompt_tokens: int = 200
    completion_tokens: int = 100
    total_tokens: int = 300


@dataclass
class FakeMessage:
    content: str


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage = field(default_factory=FakeUsage)


class Replies:
    def __init__(self, *payloads: object) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return FakeResponse(choices=[FakeChoice(FakeMessage(body))])


def context(platform: SourcePlatform = SourcePlatform.CSV) -> SourceContext:
    return SourceContext(
        platform=platform,
        base_url=str(CSV_PATH),
        store_name="vendor_catalog",
        fetched_at=FETCHED_AT,
    )


def settings_for(settings_factory: SettingsFactory) -> Any:
    settings = settings_factory(nebius_api_key="k")
    settings.connector_backoff_base = 0.0
    return settings


def test_semicolon_delimiter_is_sniffed() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    assert "Product Name" in connector.headers
    assert len(connector.headers) == 16


def test_tab_delimiter_is_sniffed() -> None:
    connector = SpreadsheetConnector(str(TSV_PATH), None)  # type: ignore[arg-type]
    assert connector.headers == ["name", "price"]
    assert connector.sample()[0]["name"] == "Tab Separated Tee"


def test_comma_delimiter_and_bom_are_handled(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_text("﻿title,price\nAlpha,10.00\n", encoding="utf-8")
    connector = SpreadsheetConnector(str(path), None)  # type: ignore[arg-type]
    assert connector.headers == ["title", "price"]


def test_blank_rows_are_dropped(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "blanks.csv"
    path.write_text("title,price\nAlpha,10\n,\nBeta,20\n", encoding="utf-8")
    assert len(SpreadsheetConnector(str(path), None).sample()) == 2  # type: ignore[arg-type]


def test_missing_file_is_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(UnsupportedSourceError, match="no such file"):
        assert SpreadsheetConnector(str(tmp_path / "nope.csv"), None).headers  # type: ignore[arg-type]


def test_empty_file_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(UnsupportedSourceError, match="is empty"):
        assert SpreadsheetConnector(str(path), None).headers  # type: ignore[arg-type]


def test_unsupported_extension_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "catalog.pdf"
    path.write_text("not a spreadsheet", encoding="utf-8")
    with pytest.raises(UnsupportedSourceError, match="unsupported spreadsheet type"):
        assert SpreadsheetConnector(str(path), None).headers  # type: ignore[arg-type]


def test_xlsx_is_read(tmp_path: pathlib.Path) -> None:
    from openpyxl import Workbook

    path = tmp_path / "catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Product Name", "Retail (GBP)", "Net weight (g)"])
    sheet.append(["Merino Jumper", 89.0, 340])
    sheet.append([None, None, None])
    workbook.save(path)

    connector = SpreadsheetConnector(str(path), None)  # type: ignore[arg-type]
    assert connector.headers == ["Product Name", "Retail (GBP)", "Net weight (g)"]
    rows = connector.sample()
    assert len(rows) == 1
    assert rows[0]["Product Name"] == "Merino Jumper"


async def test_discover_reports_the_right_platform_for_xlsx(tmp_path: pathlib.Path) -> None:
    from openpyxl import Workbook

    path = tmp_path / "catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Product Name"])
    sheet.append(["Alpha"])
    workbook.save(path)

    found = await SpreadsheetConnector(str(path), None).discover()  # type: ignore[arg-type]
    assert found.platform is SourcePlatform.XLSX
    assert found.store_name == "catalog"


async def test_discover_reports_csv_platform() -> None:
    found = await SpreadsheetConnector(str(CSV_PATH), None).discover()  # type: ignore[arg-type]
    assert found.platform is SourcePlatform.CSV
    assert found.currency is None


async def test_iter_records_yields_every_row() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    records = [record async for record in connector.iter_records()]
    assert len(records) == 4


async def test_header_mapping_is_learned_from_the_model(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(VENDOR_MAPPING.model_dump(mode="json"))
    normalizer = SpreadsheetNormalizer(LLMClient(settings_for(settings_factory), create=replies))
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    mapping = await normalizer.learn_headers(connector.headers, connector.sample())
    by_column = {m.column: m.canonical_field for m in mapping.mappings}
    assert by_column["Product Name"] == "title"
    assert by_column["Retail (GBP)"] == "price"
    assert "Cost Price" not in by_column
    assert "Warehouse Bin" not in by_column


async def test_header_prompt_carries_sample_values(settings_factory: SettingsFactory) -> None:
    replies = Replies(VENDOR_MAPPING.model_dump(mode="json"))
    normalizer = SpreadsheetNormalizer(LLMClient(settings_for(settings_factory), create=replies))
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    await normalizer.learn_headers(connector.headers, connector.sample())
    prompt = replies.calls[0]["messages"][-1]["content"]
    assert "Net weight (g)" in prompt
    assert "Merino Crew Jumper" in prompt
    assert "Cost Price" in prompt


async def test_mapping_for_an_unknown_column_is_dropped(
    settings_factory: SettingsFactory,
) -> None:
    payload = {
        "mappings": [
            {"column": "title", "canonical_field": "title", "confidence": 0.9},
            {"column": "ghost", "canonical_field": "brand", "confidence": 0.9},
        ],
        "unmapped_columns": [],
        "notes": None,
    }
    replies = Replies(payload)
    normalizer = SpreadsheetNormalizer(LLMClient(settings_for(settings_factory), create=replies))
    mapping = await normalizer.learn_headers(["title", "price"], [{"title": "A", "price": "1"}])
    assert [m.column for m in mapping.mappings] == ["title"]
    assert mapping.unmapped_columns == ["price"]


async def test_duplicate_column_mappings_are_dropped(settings_factory: SettingsFactory) -> None:
    payload = {
        "mappings": [
            {"column": "title", "canonical_field": "title", "confidence": 0.9},
            {"column": "title", "canonical_field": "brand", "confidence": 0.5},
        ],
        "unmapped_columns": [],
        "notes": None,
    }
    replies = Replies(payload)
    normalizer = SpreadsheetNormalizer(LLMClient(settings_for(settings_factory), create=replies))
    mapping = await normalizer.learn_headers(["title"], [{"title": "A"}])
    assert len(mapping.mappings) == 1
    assert mapping.mappings[0].canonical_field == "title"


async def test_mapping_to_an_invented_field_is_dropped(settings_factory: SettingsFactory) -> None:
    payload = {
        "mappings": [{"column": "title", "canonical_field": "vibe", "confidence": 0.9}],
        "unmapped_columns": [],
        "notes": None,
    }
    replies = Replies(payload)
    normalizer = SpreadsheetNormalizer(LLMClient(settings_for(settings_factory), create=replies))
    mapping = await normalizer.learn_headers(["title"], [{"title": "A"}])
    assert mapping.mappings == []
    assert mapping.unmapped_columns == ["title"]


async def test_normalizing_before_learning_headers_fails(
    settings_factory: SettingsFactory,
) -> None:
    normalizer = SpreadsheetNormalizer(
        LLMClient(settings_for(settings_factory), create=Replies({}))
    )
    with pytest.raises(NormalizationError, match="learn_headers"):
        await normalizer.normalize({"title": "A"}, context())


def test_rows_become_canonical_products() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    rows = connector.sample()
    product = build_product(rows[0], VENDOR_MAPPING, context(), row=1)
    assert product.title == "Merino Crew Jumper"
    assert product.brand == "Northfield"
    assert product.source_category == "Knitwear"
    assert product.price == Decimal("89.00")
    assert product.compare_at_price == Decimal("120.00")
    assert product.description == "A fine gauge merino jumper."
    assert product.tags == ["knitwear", "wool"]


def test_units_are_split_out_of_attribute_names() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[0], VENDOR_MAPPING, context(), row=1)
    weight = next(a for a in product.attributes if a.name == "Net weight")
    assert weight.value == "340"
    assert weight.unit == "g"
    assert weight.origin is AttributeOrigin.SOURCE


def test_ignored_columns_never_reach_the_product() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[0], VENDOR_MAPPING, context(), row=1)
    assert all("Cost" not in a.name for a in product.attributes)
    assert product.price != Decimal("41.20")
    assert "Cost Price" in product.raw


def test_variant_options_are_built_from_two_columns() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[0], VENDOR_MAPPING, context(), row=1)
    assert len(product.variants) == 1
    assert product.variants[0].options[0].name == "Size"
    assert product.variants[0].options[0].value == "S"
    assert product.variants[0].stock == 12
    assert product.variants[0].available is True


def test_zero_stock_marks_the_variant_unavailable() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[1], VENDOR_MAPPING, context(), row=2)
    assert product.variants[0].stock == 0
    assert product.variants[0].available is False


def test_multiple_images_in_one_cell_are_split() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[2], VENDOR_MAPPING, context(), row=3)
    assert len(product.images) == 2
    assert product.images[0].position == 1


def test_blank_compare_at_price_is_absent() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    product = build_product(connector.sample()[2], VENDOR_MAPPING, context(), row=3)
    assert product.compare_at_price is None


def test_a_row_without_a_title_is_rejected() -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    with pytest.raises(NormalizationError, match="no title"):
        build_product(connector.sample()[3], VENDOR_MAPPING, context(), row=4)


async def test_batch_skips_the_untitled_row(settings_factory: SettingsFactory) -> None:
    connector = SpreadsheetConnector(str(CSV_PATH), None)  # type: ignore[arg-type]
    normalizer = SpreadsheetNormalizer(
        LLMClient(settings_for(settings_factory), create=Replies({})), VENDOR_MAPPING
    )
    products = await normalizer.normalize_batch(connector.sample(), context())
    assert len(products) == 3
    assert [p.source_id for p in products] == ["row-1", "row-2", "row-3"]


def test_registry_resolves_the_spreadsheet_connector(
    settings_factory: SettingsFactory,
) -> None:
    settings = settings_factory()
    client = build_client(settings)
    for platform in (SourcePlatform.CSV, SourcePlatform.XLSX):
        found = get_connector(platform, str(CSV_PATH), settings, client)
        assert isinstance(found, SpreadsheetConnector)
