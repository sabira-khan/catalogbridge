from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from app.llm.embeddings import EmbeddingClient, cosine
from app.models.canonical import Product
from app.models.mapping import CategoryCandidate
from app.models.target import TargetSpec

WORD = re.compile(r"[a-z0-9]+")
DESCRIPTION_CHARS = 400
DEFAULT_TOP_K = 5


def product_text(product: Product) -> str:
    parts = [product.title]
    if product.source_category:
        parts.append(product.source_category)
    if product.brand:
        parts.append(product.brand)
    option_names = {
        option.name for variant in product.variants for option in variant.options
    }
    if option_names:
        parts.append(", ".join(sorted(option_names)))
    if product.tags:
        parts.append(", ".join(product.tags))
    if product.description:
        parts.append(product.description[:DESCRIPTION_CHARS])
    return ". ".join(parts)


class CandidateSource(ABC):
    name: ClassVar[str]
    requires_llm: ClassVar[bool] = False

    @abstractmethod
    async def candidates(
        self, product: Product, spec: TargetSpec, top_k: int = DEFAULT_TOP_K
    ) -> list[CategoryCandidate]: ...


class KeywordCandidates(CandidateSource):
    name = "keyword"
    requires_llm = False

    async def candidates(
        self, product: Product, spec: TargetSpec, top_k: int = DEFAULT_TOP_K
    ) -> list[CategoryCandidate]:
        tokens = set(WORD.findall(product_text(product).lower()))
        scored: list[tuple[float, CategoryCandidate]] = []
        for category in spec.categories:
            terms = category.keyword_terms()
            hits = sum(1 for term in terms if _matches(term, tokens))
            if not hits:
                continue
            score = min(1.0, hits / len(terms)) if terms else 0.0
            scored.append(
                (
                    score,
                    CategoryCandidate(
                        category_id=category.id,
                        path=category.path,
                        similarity=round(score, 6),
                        retriever=self.name,
                    ),
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1].category_id))
        return [candidate for _, candidate in scored[:top_k]]


class EmbeddingCandidates(CandidateSource):
    name = "embedding"
    requires_llm = True

    def __init__(self, embeddings: EmbeddingClient) -> None:
        self.embeddings = embeddings
        self._indexed: dict[str, list[list[float]]] = {}

    async def warm(self, spec: TargetSpec) -> None:
        if spec.id in self._indexed:
            return
        texts = [category.embedding_text() for category in spec.categories]
        self._indexed[spec.id] = await self.embeddings.embed_texts(texts, stage="index_categories")

    async def candidates(
        self, product: Product, spec: TargetSpec, top_k: int = DEFAULT_TOP_K
    ) -> list[CategoryCandidate]:
        await self.warm(spec)
        vectors = self._indexed[spec.id]
        query = (await self.embeddings.embed_texts([product_text(product)], stage="embed_product"))[
            0
        ]
        scored: list[tuple[float, CategoryCandidate]] = []
        for category, vector in zip(spec.categories, vectors, strict=True):
            score = max(0.0, cosine(query, vector))
            scored.append(
                (
                    score,
                    CategoryCandidate(
                        category_id=category.id,
                        path=category.path,
                        similarity=round(score, 6),
                        retriever=self.name,
                    ),
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1].category_id))
        return [candidate for _, candidate in scored[:top_k]]


def _matches(term: str, tokens: set[str]) -> bool:
    parts = WORD.findall(term)
    return bool(parts) and all(part in tokens for part in parts)
