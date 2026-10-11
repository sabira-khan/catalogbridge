from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.connectors.base import SourceContext
from app.models.canonical import Product
from app.models.enums import SourcePlatform


class Normalizer(ABC):
    platform: ClassVar[SourcePlatform]
    requires_llm: ClassVar[bool] = False

    @abstractmethod
    async def normalize(self, record: dict[str, Any], context: SourceContext) -> Product: ...

    async def normalize_batch(
        self, records: list[dict[str, Any]], context: SourceContext
    ) -> list[Product]:
        products: list[Product] = []
        for record in records:
            products.append(await self.normalize(record, context))
        return products
