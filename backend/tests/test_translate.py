from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from app.llm.client import LLMClient
from app.models.canonical import Product
from app.models.enums import AttributeOrigin, SourcePlatform
from app.models.mapping import MappedAttribute, MappedProduct
from app.pipeline.targets import load_target_specs
from app.pipeline.translate import Translator

from tests.conftest import SettingsFactory


@dataclass
class FakeUsage:
    prompt_tokens: int = 150
    completion_tokens: int = 60
    total_tokens: int = 210


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
        return FakeResponse(choices=[FakeChoice(FakeMessage(json.dumps(payload)))])


def mapped() -> MappedProduct:
    product = Product(
        source_id="1",
        source_platform=SourcePlatform.SHOPIFY,
        title="Merino Crew Jumper",
        brand="Northfield",
        description="A fine gauge merino jumper.",
        price=Decimal("89.00"),
    )
    return MappedProduct(
        product=product,
        target_category_id="w_knitwear",
        target_category_path=["Women", "Tops", "Knitwear"],
        category_confidence=0.9,
        category_reason="knitwear",
        mapped_attributes=[
            MappedAttribute(target_key="material", value="merino wool"),
            MappedAttribute(target_key="colour", value="navy"),
        ],
        needs_review=False,
    )


def french() -> dict[str, Any]:
    return {
        "title": "Pull col rond en merinos",
        "description": "Un pull en laine merinos de jauge fine.",
        "attributes": [
            {"key": "material", "value": "laine merinos"},
            {"key": "colour", "value": "bleu marine"},
        ],
    }


def translator(settings_factory: SettingsFactory, replies: Replies) -> Translator:
    settings = settings_factory(nebius_api_key="k")
    settings.connector_backoff_base = 0.0
    return Translator(load_target_specs()[0], LLMClient(settings, create=replies))


async def test_every_non_default_language_is_translated(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    product = await translator(settings_factory, replies).translate_product(mapped())
    assert sorted(product.translations) == ["ar", "de", "fr"]
    assert len(replies.calls) == 3


async def test_the_default_language_is_never_translated(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    product = await translator(settings_factory, replies).translate_product(mapped(), ["en", "fr"])
    assert list(product.translations) == ["fr"]
    assert len(replies.calls) == 1


async def test_a_translation_carries_title_description_and_attributes(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    product = await translator(settings_factory, replies).translate_product(mapped(), ["fr"])
    translation = product.translations["fr"]
    assert translation.title == "Pull col rond en merinos"
    assert translation.description == "Un pull en laine merinos de jauge fine."
    assert {a.name: a.value for a in translation.attributes} == {
        "material": "laine merinos",
        "colour": "bleu marine",
    }
    assert all(a.origin is AttributeOrigin.LLM for a in translation.attributes)


async def test_attributes_the_product_does_not_have_are_discarded(
    settings_factory: SettingsFactory,
) -> None:
    payload = {**french(), "attributes": [{"key": "invented", "value": "x"}]}
    replies = Replies(payload)
    product = await translator(settings_factory, replies).translate_product(mapped(), ["fr"])
    assert product.translations["fr"].attributes == []


async def test_languages_the_target_does_not_support_are_ignored(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    product = await translator(settings_factory, replies).translate_product(
        mapped(), ["fr", "ja", "pt"]
    )
    assert list(product.translations) == ["fr"]


async def test_an_empty_title_is_rejected_rather_than_stored(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies({**french(), "title": "   "})
    product = await translator(settings_factory, replies).translate_product(mapped(), ["fr"])
    assert product.translations == {}


async def test_an_existing_translation_is_not_redone(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    instance = translator(settings_factory, replies)
    product = await instance.translate_product(mapped(), ["fr"])
    await instance.translate_product(product, ["fr"])
    assert len(replies.calls) == 1


async def test_the_prompt_names_both_languages_and_protects_the_brand(
    settings_factory: SettingsFactory,
) -> None:
    replies = Replies(french())
    await translator(settings_factory, replies).translate_product(mapped(), ["de"])
    prompt = replies.calls[0]["messages"][-1]["content"]
    assert "Source language: en" in prompt
    assert "Target language: de" in prompt
    assert "The brand name is Northfield" in prompt
    assert "Merino Crew Jumper" in prompt


async def test_usage_is_attributed_per_language(settings_factory: SettingsFactory) -> None:
    replies = Replies(french())
    instance = translator(settings_factory, replies)
    await instance.translate_product(mapped(), ["fr", "de"])
    stages = instance.llm.tracker.totals().tokens_by_stage
    assert stages == {"translate:fr": 210, "translate:de": 210}


async def test_a_batch_translates_every_product(settings_factory: SettingsFactory) -> None:
    replies = Replies(french())
    products = await translator(settings_factory, replies).translate_products(
        [mapped(), mapped()], ["fr"]
    )
    assert all("fr" in product.translations for product in products)
    assert len(replies.calls) == 2


def test_a_translated_product_still_round_trips_as_json(
    settings_factory: SettingsFactory,
) -> None:
    product = mapped()
    restored = MappedProduct.model_validate(product.model_dump(mode="json"))
    assert restored == product


@pytest.mark.parametrize("language", ["fr", "de", "ar"])
async def test_each_supported_language_works(
    settings_factory: SettingsFactory, language: str
) -> None:
    replies = Replies(french())
    product = await translator(settings_factory, replies).translate_product(mapped(), [language])
    assert language in product.translations
