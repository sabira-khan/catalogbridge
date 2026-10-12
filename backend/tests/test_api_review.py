from __future__ import annotations

import pathlib
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from app.config import get_settings
from app.db import JobRow, MappedProductRow, Session, get_engine
from app.main import create_app
from app.models.canonical import Product, Variant, VariantOption
from app.models.enums import SourcePlatform
from app.models.mapping import CategoryCandidate, MappedAttribute, MappedProduct
from fastapi.testclient import TestClient

from tests.conftest import SettingsFactory


@pytest.fixture
def client(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: SettingsFactory,
) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    settings = settings_factory(nebius_api_key="k")
    assert settings.database_path.endswith("test.db")
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    with TestClient(application) as test_client:
        yield test_client


def seed(
    client: TestClient, *, confidence: float = 0.45, needs_review: bool = True
) -> tuple[str, str]:
    job_id = uuid.uuid4().hex
    product_id = uuid.uuid4().hex
    product = Product(
        source_id="1",
        source_platform=SourcePlatform.SHOPIFY,
        title="Merino Crew Jumper",
        brand="Northfield",
        source_category="Knitwear",
        description="A fine gauge merino jumper.",
        price=Decimal("89.00"),
        currency="GBP",
        variants=[
            Variant(
                sku="MCJ-S",
                options=[VariantOption(name="Size", value="S")],
                price=Decimal("89.00"),
                available=True,
            )
        ],
        raw={"id": 1, "body_html": "<p>secret</p>"},
    )
    mapped = MappedProduct(
        product=product,
        target_category_id="w_knitwear",
        target_category_path=["Women", "Tops", "Knitwear"],
        category_confidence=confidence,
        category_reason="merino jumper",
        category_candidates=[
            CategoryCandidate(category_id="w_knitwear", similarity=0.8, retriever="keyword")
        ],
        mapped_attributes=[MappedAttribute(target_key="brand", value="Northfield")],
        missing_attributes=["colour", "material", "size"],
        needs_review=needs_review,
    )
    with Session(get_engine()) as session:
        session.add(
            JobRow(
                id=job_id,
                source="examplestore.com",
                platform="shopify",
                target_id="meridian_apparel",
                status="ready",
                store_name="Example",
                currency="GBP",
                product_count=1,
                needs_review_count=1 if needs_review else 0,
            )
        )
        session.add(
            MappedProductRow(
                id=product_id,
                job_id=job_id,
                source_id="1",
                title=product.title,
                target_category_id="w_knitwear",
                category_confidence=confidence,
                needs_review=needs_review,
                payload=mapped.model_dump(mode="json", exclude={"product": {"raw"}}),
            )
        )
        session.commit()
    return job_id, product_id


def test_targets_are_listed(client: TestClient) -> None:
    response = client.get("/api/targets")
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == "meridian_apparel"
    assert len(payload[0]["categories"]) == 30


def test_one_target_exposes_required_attributes(client: TestClient) -> None:
    payload = client.get("/api/targets/meridian_apparel").json()
    dresses = next(c for c in payload["categories"] if c["id"] == "w_dresses")
    assert dresses["required_attributes"] == ["brand", "colour", "material", "size", "length"]


def test_unknown_target_is_404(client: TestClient) -> None:
    assert client.get("/api/targets/nope").status_code == 404


def test_a_job_requires_ownership_confirmation(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={"source": "examplestore.com", "target_id": "meridian_apparel"},
    )
    assert response.status_code == 400
    assert "confirmed_ownership" in response.text


def test_a_job_against_an_unknown_target_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "source": "examplestore.com",
            "target_id": "nope",
            "confirmed_ownership": True,
        },
    )
    assert response.status_code == 404


def test_jobs_and_products_are_listed(client: TestClient) -> None:
    job_id, product_id = seed(client)
    jobs = client.get("/api/jobs").json()
    assert [job["id"] for job in jobs] == [job_id]
    products = client.get(f"/api/jobs/{job_id}/products").json()
    assert products[0]["id"] == product_id
    assert products[0]["job_id"] == job_id
    assert products[0]["needs_review"] is True
    assert products[0]["missing_attributes"] == ["colour", "material", "size"]


def test_products_can_be_filtered_to_those_needing_review(client: TestClient) -> None:
    job_id, _ = seed(client)
    assert len(client.get(f"/api/jobs/{job_id}/products?needs_review=true").json()) == 1
    assert client.get(f"/api/jobs/{job_id}/products?needs_review=false").json() == []


def test_product_detail_never_exposes_the_raw_payload(client: TestClient) -> None:
    _, product_id = seed(client)
    response = client.get(f"/api/products/{product_id}")
    assert response.status_code == 200
    assert "body_html" not in response.text
    assert "secret" not in response.text


def test_product_detail_carries_variants_and_candidates(client: TestClient) -> None:
    _, product_id = seed(client)
    payload = client.get(f"/api/products/{product_id}").json()
    assert payload["variants"][0]["options"][0] == {"name": "Size", "value": "S"}
    assert payload["candidates"][0]["category_id"] == "w_knitwear"
    assert payload["description"] == "A fine gauge merino jumper."


def test_stats_summarise_the_job(client: TestClient) -> None:
    job_id, _ = seed(client)
    stats = client.get(f"/api/jobs/{job_id}/stats").json()
    assert stats == {
        "products": 1,
        "needs_review": 1,
        "reviewed": 0,
        "unmapped": 0,
        "mean_confidence": 0.45,
        "corrections": 0,
    }


def test_a_correction_clears_review_and_is_recorded(client: TestClient) -> None:
    job_id, product_id = seed(client)
    response = client.post(
        f"/api/products/{product_id}/correction",
        json={
            "target_category_id": "w_tops_tshirts",
            "attributes": {"colour": "navy", "material": "merino wool", "size": "S"},
            "note": "It is a tee, not knitwear",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["target_category_id"] == "w_tops_tshirts"
    assert payload["target_category_path"] == ["Women", "Tops", "T-shirts"]
    assert payload["category_confidence"] == 1.0
    assert payload["needs_review"] is False
    assert payload["reviewed"] is True
    assert payload["missing_attributes"] == []

    stats = client.get(f"/api/jobs/{job_id}/stats").json()
    assert stats["needs_review"] == 0
    assert stats["reviewed"] == 1
    assert stats["corrections"] == 1


def test_a_correction_to_an_unknown_category_is_rejected(client: TestClient) -> None:
    _, product_id = seed(client)
    response = client.post(
        f"/api/products/{product_id}/correction",
        json={"target_category_id": "w_spacesuits"},
    )
    assert response.status_code == 400
    assert "not a category" in response.text


def test_corrections_are_listed_for_the_job(client: TestClient) -> None:
    job_id, product_id = seed(client)
    client.post(
        f"/api/products/{product_id}/correction",
        json={"target_category_id": "w_tops_tshirts", "note": "tee"},
    )
    corrections = client.get(f"/api/jobs/{job_id}/corrections").json()
    assert len(corrections) == 1
    assert corrections[0]["from_category_id"] == "w_knitwear"
    assert corrections[0]["to_category_id"] == "w_tops_tshirts"
    assert corrections[0]["note"] == "tee"


def test_attributes_only_correction_keeps_the_category(client: TestClient) -> None:
    _, product_id = seed(client)
    payload = client.post(
        f"/api/products/{product_id}/correction",
        json={"attributes": {"colour": "navy", "material": "wool", "size": "S"}},
    ).json()
    assert payload["target_category_id"] == "w_knitwear"
    assert payload["missing_attributes"] == []
    assert {a["target_key"] for a in payload["attributes"]} == {
        "brand",
        "colour",
        "material",
        "size",
    }


def test_relearn_without_corrections_is_rejected(client: TestClient) -> None:
    job_id, _ = seed(client)
    response = client.post(f"/api/jobs/{job_id}/relearn")
    assert response.status_code == 400
    assert "nothing to learn from" in response.text


def test_export_as_json_and_csv(client: TestClient) -> None:
    job_id, _ = seed(client)
    payload = client.get(f"/api/jobs/{job_id}/export?fmt=json").json()
    assert payload["filename"].endswith(".json")
    assert "Merino Crew Jumper" in payload["body"]

    csv_payload = client.get(f"/api/jobs/{job_id}/export?fmt=csv").json()
    assert csv_payload["filename"].endswith(".csv")
    assert "MCJ-S" in csv_payload["body"]
    assert "body_html" not in csv_payload["body"]


def test_export_rejects_an_unknown_format(client: TestClient) -> None:
    job_id, _ = seed(client)
    assert client.get(f"/api/jobs/{job_id}/export?fmt=xml").status_code == 400


def test_unknown_job_and_product_are_404(client: TestClient) -> None:
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.get("/api/jobs/missing/products").status_code == 404
    assert client.get("/api/products/missing").status_code == 404


def test_health_still_works_alongside_the_api(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_usage_is_empty_for_a_job_that_made_no_calls(client: TestClient) -> None:
    job_id, _ = seed(client)
    payload = client.get(f"/api/jobs/{job_id}/usage").json()
    assert payload == {
        "calls": 0,
        "total_tokens": 0,
        "tokens_by_stage": {},
        "tokens_by_model": {},
    }


def test_usage_aggregates_stored_records(client: TestClient) -> None:
    from app.db import UsageRow

    job_id, _ = seed(client)
    with Session(get_engine()) as session:
        session.add(
            UsageRow(
                job_id=job_id, stage="map_category", model="nemotron", total_tokens=200
            )
        )
        session.add(
            UsageRow(
                job_id=job_id, stage="index_categories", model="qwen-embed", total_tokens=50
            )
        )
        session.commit()
    payload = client.get(f"/api/jobs/{job_id}/usage").json()
    assert payload["calls"] == 2
    assert payload["total_tokens"] == 250
    assert payload["tokens_by_stage"] == {"map_category": 200, "index_categories": 50}
    assert payload["tokens_by_model"] == {"nemotron": 200, "qwen-embed": 50}


def test_usage_for_an_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/jobs/missing/usage").status_code == 404
