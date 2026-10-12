from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings
from app.connectors.base import Connector, SourceContext
from app.connectors.shopify import RETRYABLE_STATUS, normalize_store_url
from app.errors import ConnectorHTTPError, StoreNotWooCommerceError
from app.logging import get_logger, log_event
from app.models.enums import SourcePlatform

STORE_API_PATHS = ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products")
SITE_INFO_PATH = "/wp-json"
MAX_PER_PAGE = 100
TOTAL_PAGES_HEADER = "X-WP-TotalPages"

logger = get_logger(__name__)


class WooCommerceConnector(Connector):
    platform = SourcePlatform.WOOCOMMERCE

    def __init__(self, source: str, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(source, settings, client)
        self.base_url = normalize_store_url(source)
        self._pages_fetched = 0
        self._products_path: str | None = None

    @property
    def pages_fetched(self) -> int:
        return self._pages_fetched

    async def discover(self) -> SourceContext:
        currency = None
        probe = await self._fetch_products_page(1, per_page=1)
        if probe:
            prices = probe[0].get("prices")
            if isinstance(prices, dict):
                code = prices.get("currency_code")
                currency = code if isinstance(code, str) else None
        return SourceContext(
            platform=self.platform,
            base_url=self.base_url,
            currency=currency,
            store_name=await self._store_name(),
            pages_fetched=self._pages_fetched,
        )

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        seen: set[str] = set()
        page = 1
        per_page = min(self.settings.connector_page_size, MAX_PER_PAGE)
        while page <= self.settings.connector_max_pages:
            products = await self._fetch_products_page(page, per_page=per_page)
            if not products:
                break
            fresh = [p for p in products if str(p.get("id")) not in seen]
            if not fresh:
                log_event(
                    logger,
                    "store ignores the page parameter, stopping pagination",
                    base_url=self.base_url,
                    page=page,
                )
                break
            self._pages_fetched = page
            for product in fresh:
                seen.add(str(product.get("id")))
                yield product
            page += 1
            if self.settings.connector_page_delay > 0:
                await asyncio.sleep(self.settings.connector_page_delay)

    async def _fetch_products_page(self, page: int, *, per_page: int) -> list[dict[str, Any]]:
        params = {"page": str(page), "per_page": str(per_page)}
        for path in self._candidate_paths():
            url = f"{self.base_url}{path}"
            response = await self._get(url, params)
            if response.status_code == httpx.codes.NOT_FOUND:
                continue
            if response.status_code >= httpx.codes.BAD_REQUEST:
                if response.status_code == httpx.codes.BAD_REQUEST and page > 1:
                    return []
                raise ConnectorHTTPError(url, response.status_code)
            payload = self._parse(response)
            self._products_path = path
            return payload
        raise StoreNotWooCommerceError(self.base_url)

    def _candidate_paths(self) -> tuple[str, ...]:
        if self._products_path is not None:
            return (self._products_path,)
        return STORE_API_PATHS

    def _parse(self, response: httpx.Response) -> list[dict[str, Any]]:
        if "json" not in response.headers.get("content-type", "").lower():
            raise StoreNotWooCommerceError(self.base_url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise StoreNotWooCommerceError(self.base_url) from exc
        if not isinstance(payload, list):
            raise StoreNotWooCommerceError(self.base_url)
        return [item for item in payload if isinstance(item, dict)]

    async def _store_name(self) -> str | None:
        try:
            response = await self._get(f"{self.base_url}{SITE_INFO_PATH}", None)
        except ConnectorHTTPError:
            return None
        if response.status_code != httpx.codes.OK:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        name = payload.get("name") if isinstance(payload, dict) else None
        return name if isinstance(name, str) and name.strip() else None

    async def _get(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        attempts = self.settings.connector_max_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                await self._backoff(attempt, None)
                continue
            if response.status_code in RETRYABLE_STATUS:
                if attempt == attempts:
                    raise ConnectorHTTPError(url, response.status_code, "retries exhausted")
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            return response
        raise ConnectorHTTPError(url, 0, str(last_error))

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.settings.connector_backoff_base * (2 ** (attempt - 1))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))
