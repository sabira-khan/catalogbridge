from __future__ import annotations

import json

from app.llm.client import LLMClient
from app.llm.prompts.loader import render_prompt
from app.logging import get_logger, log_event
from app.models.canonical import Attribute
from app.models.enums import AttributeOrigin
from app.models.llm_io import TranslationDraft
from app.models.mapping import MappedProduct, Translation
from app.models.target import TargetSpec

TRANSLATE_STAGE = "translate"

logger = get_logger(__name__)


class Translator:
    def __init__(self, spec: TargetSpec, llm: LLMClient, *, model: str | None = None) -> None:
        self.spec = spec
        self.llm = llm
        self.model = model

    async def translate_products(
        self, products: list[MappedProduct], languages: list[str] | None = None
    ) -> list[MappedProduct]:
        for product in products:
            await self.translate_product(product, languages)
        return products

    async def translate_product(
        self, mapped: MappedProduct, languages: list[str] | None = None
    ) -> MappedProduct:
        wanted = self._languages(languages)
        for language in wanted:
            if language in mapped.translations:
                continue
            draft = await self._translate(mapped, language)
            if draft is not None:
                mapped.translations[language] = draft
        return mapped

    def _languages(self, languages: list[str] | None) -> list[str]:
        requested = languages if languages is not None else self.spec.languages
        unknown = [code for code in requested if code not in self.spec.languages]
        if unknown:
            log_event(
                logger,
                "ignoring languages the target does not support",
                unsupported=unknown,
                supported=self.spec.languages,
            )
        return [
            code
            for code in requested
            if code in self.spec.languages and code != self.spec.default_language
        ]

    async def _translate(self, mapped: MappedProduct, language: str) -> Translation | None:
        payload = {
            "title": mapped.product.title,
            "description": mapped.product.description,
            "attributes": [
                {"key": attribute.target_key, "value": attribute.value}
                for attribute in mapped.mapped_attributes
            ],
        }
        draft, _ = await self.llm.complete_json(
            prompt=render_prompt(
                "translate_product",
                source_language=self.spec.default_language,
                target_language=language,
                record=json.dumps(payload, indent=2, ensure_ascii=False),
                notes=_notes(mapped),
            ),
            response_model=TranslationDraft,
            stage=f"{TRANSLATE_STAGE}:{language}",
            model=self.model,
        )
        if not draft.title.strip():
            log_event(logger, "dropped an empty translation", language=language)
            return None

        known = {attribute.target_key for attribute in mapped.mapped_attributes}
        attributes = [
            Attribute(name=item.key, value=item.value, origin=AttributeOrigin.LLM)
            for item in draft.attributes
            if item.key in known and item.value.strip()
        ]
        return Translation(
            title=draft.title.strip(),
            description=(draft.description or "").strip() or None,
            attributes=attributes,
        )


def _notes(mapped: MappedProduct) -> str:
    if not mapped.product.brand:
        return ""
    return f"The brand name is {mapped.product.brand}. Leave it exactly as written."
