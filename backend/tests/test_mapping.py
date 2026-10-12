from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from app.errors import LLMTransportError
from app.llm.client import LLMClient
from app.llm.embeddings import EmbeddingClient, cosine, normalize
from app.models.canonical import Product, Variant, VariantOption
from app.models.enums import SourcePlatform
from app.models.mapping import CorrectionExample
from app.models.target import TargetSpec
from app.pipeline.candidates import (
    EmbeddingCandidates,
    KeywordCandidates,
    product_text,
)
from app.pipeline.map import CategoryMapper
from app.pipeline.targets import load_target_specs

from tests.conftest import SettingsFactory


@dataclass
class FakeUsage:
    prompt_tokens: int = 80
    completion_tokens: int = 20
    total_tokens: int = 100


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


@dataclass
class FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class FakeEmbeddingResponse:
    data: list[FakeEmbeddingItem]
    usage: FakeUsage = field(default_factory=FakeUsage)


def selection_reply(payload: dict[str, Any]) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(FakeMessage(json.dumps(payload)))])


class Replies:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def spec() -> TargetSpec:
    return load_target_specs()[0]


def jumper() -> Product:
    return Product(
        source_id="1",
        source_platform=SourcePlatform.SHOPIFY,
        title="Merino Crew Neck Jumper",
        source_category="Knitwear",
        brand="Example",
        description="A fine gauge merino wool jumper for women.",
        tags=["knitwear", "jumper", "merino"],
        variants=[
            Variant(
                sku="MCJ-S",
                options=[VariantOption(name="Size", value="S")],
                price=Decimal("89.00"),
            )
        ],
    )


def llm(settings_factory: SettingsFactory, replies: Replies) -> LLMClient:
    settings = settings_factory(nebius_api_key="k")
    settings.connector_backoff_base = 0.0
    return LLMClient(settings, create=replies)


def test_product_text_includes_the_signals_the_retriever_needs() -> None:
    text = product_text(jumper())
    assert "Merino Crew Neck Jumper" in text
    assert "Knitwear" in text
    assert "Size" in text
    assert "merino" in text


async def test_keyword_baseline_finds_knitwear() -> None:
    candidates = await KeywordCandidates().candidates(jumper(), spec())
    assert candidates
    assert candidates[0].category_id in {"w_knitwear", "m_knitwear"}
    assert candidates[0].retriever == "keyword"
    assert 0.0 < candidates[0].similarity <= 1.0


async def test_keyword_baseline_returns_nothing_for_an_unrelated_product() -> None:
    product = Product(
        source_id="2",
        source_platform=SourcePlatform.SHOPIFY,
        title="Industrial Hydraulic Pump Seal Kit",
    )
    assert await KeywordCandidates().candidates(product, spec()) == []


async def test_keyword_baseline_respects_top_k() -> None:
    candidates = await KeywordCandidates().candidates(jumper(), spec(), top_k=2)
    assert len(candidates) <= 2


def test_normalize_produces_a_unit_vector() -> None:
    vector = normalize([3.0, 4.0])
    assert vector == [0.6, 0.8]
    assert cosine(vector, vector) == pytest.approx(1.0)


def test_normalize_tolerates_a_zero_vector() -> None:
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


def test_cosine_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        cosine([1.0], [1.0, 0.0])


async def test_embedding_candidates_rank_by_similarity(
    settings_factory: SettingsFactory,
) -> None:
    target = spec()
    knitwear_index = next(
        i for i, c in enumerate(target.categories) if c.id == "w_knitwear"
    )

    async def embed(**kwargs: Any) -> FakeEmbeddingResponse:
        texts = kwargs["input"]
        vectors = []
        for text in texts:
            if "Merino Crew Neck Jumper" in text or "Knitwear" in text and "Women" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return FakeEmbeddingResponse(data=[FakeEmbeddingItem(v) for v in vectors])

    settings = settings_factory(nebius_api_key="k")
    source = EmbeddingCandidates(EmbeddingClient(settings, embed=embed))
    candidates = await source.candidates(jumper(), target, top_k=3)
    assert candidates[0].category_id == target.categories[knitwear_index].id
    assert candidates[0].retriever == "embedding"
    assert candidates[0].similarity == pytest.approx(1.0)


async def test_category_embeddings_are_indexed_once(settings_factory: SettingsFactory) -> None:
    calls: list[list[str]] = []

    async def embed(**kwargs: Any) -> FakeEmbeddingResponse:
        calls.append(kwargs["input"])
        return FakeEmbeddingResponse(
            data=[FakeEmbeddingItem([1.0, 0.0]) for _ in kwargs["input"]]
        )

    settings = settings_factory(nebius_api_key="k")
    source = EmbeddingCandidates(EmbeddingClient(settings, embed=embed, batch_size=100))
    target = spec()
    await source.candidates(jumper(), target)
    first_call_count = len(calls)
    await source.candidates(jumper(), target)
    assert len(calls) == first_call_count


async def test_embedding_response_with_wrong_vector_count_is_rejected(
    settings_factory: SettingsFactory,
) -> None:
    async def embed(**kwargs: Any) -> FakeEmbeddingResponse:
        return FakeEmbeddingResponse(data=[FakeEmbeddingItem([1.0, 0.0])])

    settings = settings_factory(nebius_api_key="k")
    client = EmbeddingClient(settings, embed=embed)
    with pytest.raises(LLMTransportError, match="expected 2"):
        await client.embed_texts(["a", "b"], stage="test")


async def test_mapper_builds_a_confident_mapping(settings_factory: SettingsFactory) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.93,
                "reason": "Merino crew neck jumper, so knitwear",
                "attributes": [
                    {"target_key": "brand", "value": "Example", "unit": None, "confidence": 1.0},
                    {"target_key": "colour", "value": "navy", "unit": None, "confidence": 0.6},
                    {
                        "target_key": "material",
                        "value": "merino wool",
                        "unit": None,
                        "confidence": 0.95,
                    },
                    {"target_key": "size", "value": "S", "unit": None, "confidence": 0.9},
                ],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.target_category_id == "w_knitwear"
    assert mapped.target_category_path == ["Women", "Tops", "Knitwear"]
    assert mapped.category_confidence == 0.93
    assert mapped.missing_attributes == []
    assert mapped.needs_review is False
    assert {a.target_key for a in mapped.mapped_attributes} == {
        "brand",
        "colour",
        "material",
        "size",
    }


async def test_low_confidence_is_flagged_for_review(settings_factory: SettingsFactory) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.4,
                "reason": "Could be knitwear or a shirt",
                "attributes": [
                    {"target_key": "brand", "value": "Example", "unit": None, "confidence": 1.0},
                    {"target_key": "colour", "value": "navy", "unit": None, "confidence": 0.5},
                    {"target_key": "material", "value": "wool", "unit": None, "confidence": 0.5},
                    {"target_key": "size", "value": "S", "unit": None, "confidence": 0.5},
                ],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.needs_review is True


async def test_missing_required_attributes_force_review(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.95,
                "reason": "Clearly knitwear",
                "attributes": [
                    {"target_key": "brand", "value": "Example", "unit": None, "confidence": 1.0}
                ],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.needs_review is True
    assert mapped.missing_attributes == ["colour", "material", "size"]


async def test_unmappable_product_is_left_uncategorised(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": None,
                "unmappable": True,
                "confidence": 0.0,
                "reason": "This is a hydraulic seal kit, not apparel",
                "attributes": [],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.target_category_id is None
    assert mapped.needs_review is True
    assert "hydraulic seal kit" in mapped.category_reason


async def test_hallucinated_category_is_rejected(settings_factory: SettingsFactory) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_spacesuits",
                "unmappable": False,
                "confidence": 0.99,
                "reason": "Confidently wrong",
                "attributes": [],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.target_category_id is None
    assert mapped.needs_review is True
    assert "was not a candidate" in mapped.category_reason


async def test_attribute_value_outside_allowed_set_is_dropped(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.9,
                "reason": "Knitwear",
                "attributes": [
                    {"target_key": "brand", "value": "Example", "unit": None, "confidence": 1.0},
                    {
                        "target_key": "colour",
                        "value": "chartreuse",
                        "unit": None,
                        "confidence": 0.9,
                    },
                    {"target_key": "material", "value": "wool", "unit": None, "confidence": 0.9},
                    {"target_key": "size", "value": "S", "unit": None, "confidence": 0.9},
                ],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert "colour" not in {a.target_key for a in mapped.mapped_attributes}
    assert "colour" in mapped.missing_attributes
    assert mapped.needs_review is True


async def test_undeclared_attributes_are_discarded(settings_factory: SettingsFactory) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.9,
                "reason": "Knitwear",
                "attributes": [
                    {"target_key": "vibe", "value": "cosy", "unit": None, "confidence": 0.9}
                ],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(jumper())
    assert mapped.mapped_attributes == []


async def test_product_with_no_candidates_is_not_sent_to_the_model(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(selection_reply({"confidence": 0.9, "reason": "unused"}))
    product = Product(
        source_id="3",
        source_platform=SourcePlatform.SHOPIFY,
        title="Industrial Hydraulic Pump Seal Kit",
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_product(product)
    assert mapped.target_category_id is None
    assert mapped.needs_review is True
    assert replies.calls == []


async def test_candidates_and_required_attributes_reach_the_prompt(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.9,
                "reason": "Knitwear",
                "attributes": [],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    await mapper.map_product(jumper())
    prompt = replies.calls[0]["messages"][-1]["content"]
    assert "w_knitwear: Women > Tops > Knitwear" in prompt
    assert "required: size (enum) one of: XXS, XS, S, M, L, XL, XXL, XXXL" in prompt
    assert "Merino Crew Neck Jumper" in prompt


async def test_reviewer_corrections_are_fed_back_into_the_prompt(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.9,
                "reason": "Knitwear",
                "attributes": [],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    await mapper.map_product(
        jumper(),
        [
            CorrectionExample(
                title="Lambswool Cardigan",
                category_id="w_knitwear",
                category_path=["Women", "Tops", "Knitwear"],
            )
        ],
    )
    prompt = replies.calls[0]["messages"][-1]["content"]
    assert "A human reviewer already corrected" in prompt
    assert "Lambswool Cardigan -> w_knitwear" in prompt


async def test_mapping_a_batch_returns_one_result_per_product(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(
        selection_reply(
            {
                "category_id": "w_knitwear",
                "unmappable": False,
                "confidence": 0.9,
                "reason": "Knitwear",
                "attributes": [],
            }
        )
    )
    mapper = CategoryMapper(spec(), llm(settings_factory, replies), KeywordCandidates())
    mapped = await mapper.map_products([jumper(), jumper()])
    assert len(mapped) == 2
    assert len(replies.calls) == 2
