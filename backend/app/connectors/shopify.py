from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.connectors.base import Connector, SourceContext
from app.errors import (
    ConnectorHTTPError,
    StoreNotShopifyError,
    StorePasswordProtectedError,
    UnsupportedSourceError,
)
from app.logging import get_logger, log_event
from app.models.enums import SourcePlatform

PRODUCTS_PATH = "/products.json"
META_PATH = "/meta.json"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

logger = get_logger(__name__)


def normalize_store_url(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise UnsupportedSourceError("store URL is empty")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise UnsupportedSourceError(f"unsupported URL scheme: {parsed.scheme}")
    host = parsed.netloc.lower()
    if host.endswith(":443"):
        host = host.removesuffix(":443")
    if not host or "." not in host.split(":")[0]:
        raise UnsupportedSourceError(f"not a usable store URL: {raw}")
    return f"https://{host}"


class ShopifyConnector(Connector):
    platform = SourcePlatform.SHOPIFY

    def __init__(self, source: str, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(source, settings, client)
        self.base_url = normalize_store_url(source)
        self._pages_fetched = 0

    @property
    def pages_fetched(self) -> int:
        return self._pages_fetched

    async def discover(self) -> SourceContext:
        currency, store_name = await self._fetch_meta()
        return SourceContext(
            platform=self.platform,
            base_url=self.base_url,
            currency=currency,
            store_name=store_name,
            pages_fetched=self._pages_fetched,
        )

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        seen: set[str] = set()
        page = 1
        while page <= self.settings.connector_max_pages:
            products = await self._fetch_products_page(page)
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

    async def _fetch_products_page(self, page: int) -> list[dict[str, Any]]:
        url = f"{self.base_url}{PRODUCTS_PATH}"
        response = await self._get(
            url, {"limit": str(self.settings.connector_page_size), "page": str(page)}
        )
        self._guard_not_shopify(response, url)
        payload = self._parse_json(response, url)
        products = payload.get("products")
        if not isinstance(products, list):
            raise StoreNotShopifyError(self.base_url)
        return [p for p in products if isinstance(p, dict)]

    async def _fetch_meta(self) -> tuple[str | None, str | None]:
        url = f"{self.base_url}{META_PATH}"
        try:
            response = await self._get(url, None)
            if response.status_code != httpx.codes.OK:
                return None, None
            payload = self._parse_json(response, url)
        except (ConnectorHTTPError, StoreNotShopifyError):
            log_event(logger, "shop currency lookup failed", base_url=self.base_url)
            return None, None
        currency = payload.get("currency")
        name = payload.get("name")
        return (
            currency if isinstance(currency, str) else None,
            name if isinstance(name, str) else None,
        )

    def _guard_not_shopify(self, response: httpx.Response, url: str) -> None:
        if response.url.path.rstrip("/").endswith("/password"):
            raise StorePasswordProtectedError(self.base_url)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise StoreNotShopifyError(self.base_url)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ConnectorHTTPError(url, response.status_code)

    def _parse_json(self, response: httpx.Response, url: str) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise StoreNotShopifyError(self.base_url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise StoreNotShopifyError(self.base_url) from exc
        if not isinstance(payload, dict):
            raise StoreNotShopifyError(self.base_url)
        return payload

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
