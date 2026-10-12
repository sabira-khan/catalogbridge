from __future__ import annotations

import json

from app.llm.client import LLMClient
from app.llm.prompts.loader import render_prompt
from app.logging import get_logger, log_event
from app.models.canonical import Product
from app.models.llm_io import CategorySelection
from app.models.mapping import CategoryCandidate, CorrectionExample, MappedAttribute, MappedProduct
from app.models.target import TargetAttribute, TargetSpec
from app.pipeline.candidates import DEFAULT_TOP_K, CandidateSource

MAP_STAGE = "map_category"
logger = get_logger(__name__)


class CategoryMapper:
    def __init__(
        self,
        spec: TargetSpec,
        llm: LLMClient,
        candidate_source: CandidateSource,
        *,
        top_k: int = DEFAULT_TOP_K,
        model: str | None = None,
    ) -> None:
        self.spec = spec
        self.llm = llm
        self.candidate_source = candidate_source
        self.top_k = top_k
        self.model = model

    async def map_products(
        self, products: list[Product], examples: list[CorrectionExample] | None = None
    ) -> list[MappedProduct]:
        return [await self.map_product(product, examples) for product in products]

    async def map_product(
        self, product: Product, examples: list[CorrectionExample] | None = None
    ) -> MappedProduct:
        candidates = await self.candidate_source.candidates(product, self.spec, self.top_k)
        if not candidates:
            return self._unmapped(
                product, candidates, "no candidate categories were retrieved for this product"
            )

        selection, _ = await self.llm.complete_json(
            prompt=self._prompt(product, candidates, examples or []),
            response_model=CategorySelection,
            stage=MAP_STAGE,
            model=self.model,
        )
        return self._build(product, candidates, selection)

    def _prompt(
        self,
        product: Product,
        candidates: list[CategoryCandidate],
        examples: list[CorrectionExample],
    ) -> str:
        return render_prompt(
            "map_category",
            candidates=_render_candidates(candidates),
            attributes=self._render_attributes(candidates),
            product=json.dumps(product.for_llm(), indent=2, ensure_ascii=False),
            examples=_render_examples(examples),
        )

    def _render_attributes(self, candidates: list[CategoryCandidate]) -> str:
        blocks: list[str] = []
        for candidate in candidates:
            required = self.spec.required_attributes(candidate.category_id)
            optional = [
                a
                for a in self.spec.attributes_for(candidate.category_id)
                if not a.required
            ]
            lines = [f"{candidate.category_id}:"]
            lines.extend(f"  required: {_render_attribute(a)}" for a in required)
            lines.extend(f"  optional: {_render_attribute(a)}" for a in optional)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _build(
        self,
        product: Product,
        candidates: list[CategoryCandidate],
        selection: CategorySelection,
    ) -> MappedProduct:
        allowed = {candidate.category_id for candidate in candidates}
        if selection.unmappable or selection.category_id is None:
            return self._unmapped(product, candidates, selection.reason)
        if selection.category_id not in allowed:
            log_event(
                logger,
                "model returned a category outside the candidate list",
                returned=selection.category_id,
                title=product.title,
            )
            return self._unmapped(
                product,
                candidates,
                f"model returned {selection.category_id}, which was not a candidate",
            )

        category = self.spec.category(selection.category_id)
        attributes = _valid_attributes(selection, self.spec, selection.category_id)
        supplied = {attribute.target_key for attribute in attributes}
        missing = [
            attribute.key
            for attribute in self.spec.required_attributes(selection.category_id)
            if attribute.key not in supplied
        ]
        return MappedProduct(
            product=product,
            target_category_id=selection.category_id,
            target_category_path=category.path if category else [],
            category_confidence=selection.confidence,
            category_reason=selection.reason,
            category_candidates=candidates,
            mapped_attributes=attributes,
            missing_attributes=missing,
            needs_review=self.spec.needs_review(selection.confidence) or bool(missing),
        )

    def _unmapped(
        self, product: Product, candidates: list[CategoryCandidate], reason: str
    ) -> MappedProduct:
        return MappedProduct(
            product=product,
            target_category_id=None,
            category_confidence=0.0,
            category_reason=reason,
            category_candidates=candidates,
            missing_attributes=[a.key for a in self.spec.required_attributes(None)],
            needs_review=True,
        )


def _valid_attributes(
    selection: CategorySelection, spec: TargetSpec, category_id: str
) -> list[MappedAttribute]:
    declared = {a.key: a for a in spec.attributes_for(category_id)}
    kept: list[MappedAttribute] = []
    for draft in selection.attributes:
        target = declared.get(draft.target_key)
        if target is None:
            continue
        if target.allowed_values and draft.value not in target.allowed_values:
            log_event(
                logger,
                "dropped an attribute value outside its allowed set",
                key=draft.target_key,
                value=draft.value,
            )
            continue
        kept.append(
            MappedAttribute(
                target_key=draft.target_key,
                value=draft.value,
                unit=draft.unit or target.unit,
                confidence=draft.confidence,
            )
        )
    return kept


def _render_candidates(candidates: list[CategoryCandidate]) -> str:
    return "\n".join(
        f"- {candidate.category_id}: {' > '.join(candidate.path)} "
        f"(similarity {candidate.similarity:.3f})"
        for candidate in candidates
    )


def _render_attribute(attribute: TargetAttribute) -> str:
    parts = [f"{attribute.key} ({attribute.type.value}"]
    if attribute.unit:
        parts.append(f", unit {attribute.unit}")
    parts.append(")")
    rendered = "".join(parts)
    if attribute.allowed_values:
        rendered += f" one of: {', '.join(attribute.allowed_values)}"
    return rendered


def _render_examples(examples: list[CorrectionExample]) -> str:
    if not examples:
        return ""
    lines = [
        "A human reviewer already corrected these products in this same batch.",
        "Follow the pattern they established where it applies:",
        "",
    ]
    lines.extend(
        f"- {example.title} -> {example.category_id} ({' > '.join(example.category_path)})"
        for example in examples
    )
    return "\n".join(lines)
