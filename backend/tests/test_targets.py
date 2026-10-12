from __future__ import annotations

import csv
import io
import json
import pathlib
from decimal import Decimal

import pytest
import yaml
from app.cli import app
from app.errors import TargetSpecError, UnsupportedTargetFormatError
from app.models.canonical import Product, Variant, VariantOption
from app.models.enums import SourcePlatform
from app.models.mapping import MappedAttribute, MappedProduct
from app.models.target import AttributeType, TargetAttribute, TargetCategory, TargetSpec
from app.pipeline.load import CsvLoader, JsonLoader, get_loader
from app.pipeline.targets import TARGETS_DIR, find_target_spec, load_target_spec, load_target_specs
from pydantic import ValidationError
from typer.testing import CliRunner

runner = CliRunner()

MINIMAL = {
    "id": "demo",
    "name": "Demo",
    "languages": ["en"],
    "default_language": "en",
    "categories": [{"id": "c1", "name": "Shirts", "path": ["Tops", "Shirts"]}],
}


def write_spec(tmp_path: pathlib.Path, payload: object, name: str = "demo.yaml") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_shipped_apparel_spec_loads_and_validates() -> None:
    specs = load_target_specs()
    assert len(specs) == 1
    spec = specs[0]
    assert spec.id == "meridian_apparel"
    assert len(spec.categories) == 22
    assert spec.default_language == "en"
    assert "ar" in spec.languages


def test_shipped_spec_lives_where_claude_md_says() -> None:
    assert TARGETS_DIR.is_dir()
    assert (TARGETS_DIR / "meridian_apparel.yaml").is_file()


def test_required_attributes_merge_global_and_category() -> None:
    spec = load_target_specs()[0]
    keys = [a.key for a in spec.required_attributes("w_dresses")]
    assert keys == ["brand", "colour", "material", "size", "length"]


def test_required_attributes_without_a_category_are_global_only() -> None:
    spec = load_target_specs()[0]
    assert [a.key for a in spec.required_attributes(None)] == ["brand", "colour", "material"]


def test_unknown_category_id_resolves_to_none() -> None:
    assert load_target_specs()[0].category("not_a_category") is None


def test_embedding_text_combines_path_and_keywords() -> None:
    category = TargetCategory(
        id="c1", name="Jeans", path=["Women", "Jeans"], keywords=["denim", "selvedge"]
    )
    assert category.embedding_text() == "Women > Jeans. denim, selvedge"
    assert category.label == "Women > Jeans"


def test_keyword_terms_exclude_the_path_to_keep_the_baseline_discriminative() -> None:
    category = TargetCategory(id="c1", name="Jeans", path=["Women", "Jeans"], keywords=["Denim"])
    assert category.keyword_terms() == ["jeans", "denim"]


def test_needs_review_follows_the_threshold() -> None:
    spec = load_target_specs()[0]
    assert spec.needs_review(0.69) is True
    assert spec.needs_review(0.70) is False


def test_duplicate_category_ids_are_rejected() -> None:
    payload = {
        **MINIMAL,
        "categories": [
            {"id": "c1", "name": "A", "path": ["A"]},
            {"id": "c1", "name": "B", "path": ["B"]},
        ],
    }
    with pytest.raises(ValidationError, match="duplicate category ids"):
        TargetSpec.model_validate(payload)


def test_default_language_must_be_in_languages() -> None:
    with pytest.raises(ValidationError, match="default_language"):
        TargetSpec.model_validate({**MINIMAL, "default_language": "fr"})


def test_category_path_cannot_be_empty() -> None:
    payload = {**MINIMAL, "categories": [{"id": "c1", "name": "A", "path": []}]}
    with pytest.raises(ValidationError):
        TargetSpec.model_validate(payload)


def test_spec_needs_at_least_one_category() -> None:
    with pytest.raises(ValidationError):
        TargetSpec.model_validate({**MINIMAL, "categories": []})


def test_enum_attribute_without_values_is_rejected() -> None:
    with pytest.raises(ValidationError, match="allowed_values"):
        TargetAttribute(key="size", label="Size", type=AttributeType.ENUM)


def test_malformed_yaml_raises_target_spec_error(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("id: demo\n  bad indent: true\n", encoding="utf-8")
    with pytest.raises(TargetSpecError, match="not valid YAML"):
        load_target_spec(path)


def test_non_mapping_yaml_raises_target_spec_error(tmp_path: pathlib.Path) -> None:
    path = write_spec(tmp_path, ["not", "a", "mapping"])
    with pytest.raises(TargetSpecError, match="must be a mapping"):
        load_target_spec(path)


def test_invalid_spec_reports_the_file_path(tmp_path: pathlib.Path) -> None:
    path = write_spec(tmp_path, {**MINIMAL, "languages": []})
    with pytest.raises(TargetSpecError) as excinfo:
        load_target_spec(path)
    assert str(path) in str(excinfo.value)


def test_missing_file_raises_target_spec_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(TargetSpecError, match="cannot read file"):
        load_target_spec(tmp_path / "absent.yaml")


def test_find_target_spec_by_id(tmp_path: pathlib.Path) -> None:
    write_spec(tmp_path, MINIMAL)
    assert find_target_spec("demo", tmp_path) is not None
    assert find_target_spec("nope", tmp_path) is None


def test_empty_targets_directory_yields_no_specs(tmp_path: pathlib.Path) -> None:
    assert load_target_specs(tmp_path) == []


def _mapped() -> MappedProduct:
    product = Product(
        source_id="1",
        source_platform=SourcePlatform.SHOPIFY,
        title="Merino Crew Jumper",
        handle="merino-crew-jumper",
        brand="Example",
        currency="EUR",
        description="Soft knit",
        variants=[
            Variant(
                sku="MCJ-S-NAVY",
                title="S / Navy",
                options=[
                    VariantOption(name="Size", value="S"),
                    VariantOption(name="Colour", value="Navy"),
                ],
                price=Decimal("89.00"),
                available=True,
            ),
            Variant(sku="MCJ-M-NAVY", price=Decimal("89.00"), available=False),
        ],
        raw={"id": 1, "body_html": "<p>Soft knit</p>"},
    )
    return MappedProduct(
        product=product,
        target_category_id="w_knitwear",
        target_category_path=["Women", "Tops", "Knitwear"],
        category_confidence=0.91,
        category_reason="Merino crew jumper is knitwear",
        mapped_attributes=[
            MappedAttribute(target_key="brand", value="Example"),
            MappedAttribute(target_key="colour", value="navy"),
            MappedAttribute(target_key="material", value="merino wool"),
            MappedAttribute(target_key="knit_gauge", value="fine"),
            MappedAttribute(target_key="weight", value="250", unit="g"),
        ],
        needs_review=False,
    )


def test_json_loader_emits_a_summary_and_hides_raw() -> None:
    spec = load_target_specs()[0]
    payload = json.loads(JsonLoader().dump([_mapped()], spec))
    assert payload["target"]["id"] == "meridian_apparel"
    assert payload["summary"] == {
        "products": 1,
        "needs_review": 0,
        "mean_confidence": 0.91,
        "unmapped": 0,
        "confidence_threshold": 0.7,
    }
    assert "raw" not in payload["products"][0]["product"]
    assert "body_html" not in json.dumps(payload)


def test_csv_loader_writes_one_row_per_variant() -> None:
    spec = load_target_specs()[0]
    rows = list(csv.DictReader(io.StringIO(CsvLoader().dump([_mapped()], spec))))
    assert len(rows) == 2
    assert rows[0]["sku"] == "MCJ-S-NAVY"
    assert rows[0]["option1_name"] == "Size"
    assert rows[0]["option1_value"] == "S"
    assert rows[0]["option2_value"] == "Navy"
    assert rows[0]["available"] == "yes"
    assert rows[1]["available"] == "no"
    assert rows[1]["option1_name"] == ""


def test_csv_loader_repeats_product_columns_on_every_variant_row() -> None:
    spec = load_target_specs()[0]
    rows = list(csv.DictReader(io.StringIO(CsvLoader().dump([_mapped()], spec))))
    for row in rows:
        assert row["title"] == "Merino Crew Jumper"
        assert row["target_category_path"] == "Women > Tops > Knitwear"
        assert row["category_confidence"] == "0.91"
        assert row["needs_review"] == "no"
        assert row["currency"] == "EUR"


def test_csv_loader_gives_global_attributes_their_own_columns() -> None:
    spec = load_target_specs()[0]
    text = CsvLoader().dump([_mapped()], spec)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert "brand" in rows[0]
    assert rows[0]["colour"] == "navy"
    assert rows[0]["material"] == "merino wool"


def test_csv_loader_puts_unknown_attributes_in_the_extras_column() -> None:
    spec = load_target_specs()[0]
    rows = list(csv.DictReader(io.StringIO(CsvLoader().dump([_mapped()], spec))))
    extras = rows[0]["extra_attributes"]
    assert "knit_gauge=fine" in extras
    assert "weight=250 g" in extras


def test_csv_loader_handles_a_product_with_no_variants() -> None:
    spec = load_target_specs()[0]
    mapped = _mapped()
    mapped.product.variants = []
    rows = list(csv.DictReader(io.StringIO(CsvLoader().dump([mapped], spec))))
    assert len(rows) == 1
    assert rows[0]["sku"] == ""
    assert rows[0]["title"] == "Merino Crew Jumper"


def test_loader_registry_resolves_both_formats() -> None:
    assert isinstance(get_loader("json"), JsonLoader)
    assert isinstance(get_loader("csv"), CsvLoader)


def test_loader_registry_rejects_an_unknown_format() -> None:
    with pytest.raises(UnsupportedTargetFormatError):
        get_loader("xml")


def test_targets_command_lists_the_shipped_spec() -> None:
    result = runner.invoke(app, ["targets"])
    assert result.exit_code == 0, result.output
    assert "meridian_apparel" in result.output
    assert "Export formats: csv, json" in result.output


def test_targets_command_shows_one_category_tree() -> None:
    result = runner.invoke(app, ["targets", "--id", "meridian_apparel"])
    assert result.exit_code == 0, result.output
    assert "Women > Tops > Knitwear" in result.output
    assert "Required everywhere: brand, colour, material" in result.output


def test_targets_command_rejects_an_unknown_id() -> None:
    result = runner.invoke(app, ["targets", "--id", "nope"])
    assert result.exit_code == 1
    assert "No target spec with id nope" in result.output
