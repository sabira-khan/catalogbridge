from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.models.enums import SourcePlatform


class SourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: SourcePlatform
    base_url: str
    currency: str | None = None
    store_name: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    pages_fetched: int = 0


class Connector(ABC):
    platform: ClassVar[SourcePlatform]

    def __init__(self, source: str, settings: Settings, client: httpx.AsyncClient) -> None:
        self.source = source
        self.settings = settings
        self.client = client

    @abstractmethod
    async def discover(self) -> SourceContext: ...

    @abstractmethod
    def iter_records(self) -> AsyncIterator[dict[str, Any]]: ...
