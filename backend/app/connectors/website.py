from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.connectors.base import Connector, SourceContext
from app.connectors.jsonld import extract_jsonld_blocks, find_products
from app.connectors.robots import RobotsPolicy
from app.connectors.shopify import RETRYABLE_STATUS, normalize_store_url
from app.errors import ConnectorHTTPError, RobotsDisallowedError
from app.logging import get_logger, log_event
from app.models.enums import SourcePlatform

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
PRODUCT_URL_HINTS = ("/product/", "/products/", "/p/", "/item/", "/shop/")
SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
MAX_HTML_BYTES = 400_000
DEFAULT_MAX_PAGES = 40

logger = get_logger(__name__)


class WebsiteConnector(Connector):
    platform = SourcePlatform.WEBSITE

    def __init__(self, source: str, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(source, settings, client)
        self.base_url = normalize_store_url(source)
        self.robots = RobotsPolicy(self.base_url, settings.http_user_agent)
        self._pages_fetched = 0
        self._entry_is_product_page = _looks_like_product_url(source)

    @property
    def pages_fetched(self) -> int:
        return self._pages_fetched

    async def discover(self) -> SourceContext:
        await self.robots.load(self.client)
        html = await self._fetch_html(self.base_url)
        return SourceContext(
            platform=self.platform,
            base_url=self.base_url,
            currency=None,
            store_name=_page_title(html) if html else None,
            pages_fetched=self._pages_fetched,
        )

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        await self.robots.load(self.client)
        seen_urls: set[str] = set()
        seen_products: set[str] = set()
        limit = min(self.settings.connector_max_pages, DEFAULT_MAX_PAGES)

        for url in await self._product_urls(limit):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if not self.robots.allows(url):
                log_event(logger, "robots.txt disallows this path, skipping", url=url)
                continue
            html = await self._fetch_html(url)
            if not html:
                continue
            for record in self._records_from_html(html, url):
                key = _product_key(record, url)
                if key in seen_products:
                    continue
                seen_products.add(key)
                yield record
            await self._polite_delay()

    async def _product_urls(self, limit: int) -> list[str]:
        if self._entry_is_product_page:
            entry = _with_scheme(self.source)
            return [entry]
        urls = await self._sitemap_urls(limit)
        if urls:
            return urls
        log_event(logger, "no sitemap, falling back to the entry page only", base=self.base_url)
        return [self.base_url]

    async def _sitemap_urls(self, limit: int) -> list[str]:
        for path in SITEMAP_PATHS:
            url = f"{self.base_url}{path}"
            if not self.robots.allows(url):
                continue
            body = await self._fetch_text(url)
            if not body:
                continue
            locations = [match.strip() for match in SITEMAP_LOC.findall(body)]
            nested = [loc for loc in locations if loc.endswith((".xml", ".xml.gz"))]
            products = [loc for loc in locations if _looks_like_product_url(loc)]
            for child in nested[:3]:
                if len(products) >= limit:
                    break
                child_body = await self._fetch_text(child)
                if child_body:
                    products.extend(
                        loc
                        for loc in SITEMAP_LOC.findall(child_body)
                        if _looks_like_product_url(loc)
                    )
            if products:
                return products[:limit]
        return []

    def _records_from_html(self, html: str, url: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for block in extract_jsonld_blocks(html):
            for product in find_products(block):
                product.setdefault("url", url)
                records.append(product)
        if records:
            return records
        log_event(logger, "no product JSON-LD found, needs llm extraction", url=url)
        return [
            {
                "_extraction": "html",
                "url": url,
                "title_hint": _page_title(html),
                "html": html[:MAX_HTML_BYTES],
            }
        ]

    async def _fetch_html(self, url: str) -> str | None:
        if not self.robots.allows(url):
            raise RobotsDisallowedError(url)
        response = await self._get(url)
        if response is None or response.status_code != httpx.codes.OK:
            return None
        if "html" not in response.headers.get("content-type", "").lower():
            return None
        self._pages_fetched += 1
        return response.text

    async def _fetch_text(self, url: str) -> str | None:
        response = await self._get(url)
        if response is None or response.status_code != httpx.codes.OK:
            return None
        self._pages_fetched += 1
        return response.text

    async def _get(self, url: str) -> httpx.Response | None:
        attempts = self.settings.connector_max_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.get(url, headers={"Accept": "text/html,*/*"})
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    log_event(logger, "giving up on a page", url=url, error=str(exc))
                    return None
                await self._backoff(attempt, None)
                continue
            if response.status_code in RETRYABLE_STATUS:
                if attempt == attempts:
                    raise ConnectorHTTPError(url, response.status_code, "retries exhausted")
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            return response
        return None

    async def _polite_delay(self) -> None:
        delay = max(self.robots.crawl_delay or 0.0, self.settings.connector_page_delay)
        if delay > 0:
            await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.settings.connector_backoff_base * (2 ** (attempt - 1))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))


def _product_key(record: dict[str, Any], url: str) -> str:
    for key in ("sku", "mpn", "productID", "gtin13", "@id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    name = record.get("name")
    if isinstance(name, str) and name.strip():
        return f"name:{url}:{name.strip()}"
    return f"url:{url}"


def _looks_like_product_url(url: str) -> bool:
    path = urlparse(_with_scheme(url)).path.lower()
    return any(hint in path for hint in PRODUCT_URL_HINTS)


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"https://{url}"


def _page_title(html: str | None) -> str | None:
    if not html:
        return None
    match = TITLE_TAG.search(html)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None
