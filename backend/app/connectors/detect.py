from __future__ import annotations

from pathlib import Path

import httpx

from app.config import Settings
from app.connectors.shopify import META_PATH, PRODUCTS_PATH, normalize_store_url
from app.connectors.spreadsheet import CSV_SUFFIXES, XLSX_SUFFIXES
from app.connectors.woocommerce import STORE_API_PATHS
from app.logging import get_logger, log_event
from app.models.enums import SourcePlatform

logger = get_logger(__name__)


def detect_local(source: str) -> SourcePlatform | None:
    candidate = Path(source).expanduser()
    if "://" in source or not candidate.exists():
        return None
    suffix = candidate.suffix.lower()
    if suffix in XLSX_SUFFIXES:
        return SourcePlatform.XLSX
    if suffix in CSV_SUFFIXES or not suffix:
        return SourcePlatform.CSV
    return None


async def detect_source(
    source: str, settings: Settings, client: httpx.AsyncClient
) -> SourcePlatform:
    local = detect_local(source)
    if local is not None:
        log_event(logger, "detected a local spreadsheet", source=source, platform=local)
        return local

    base = normalize_store_url(source)
    if await _is_shopify(base, client):
        log_event(logger, "detected a Shopify storefront", base_url=base)
        return SourcePlatform.SHOPIFY
    if await _is_woocommerce(base, client):
        log_event(logger, "detected a WooCommerce storefront", base_url=base)
        return SourcePlatform.WOOCOMMERCE
    log_event(logger, "no known platform, falling back to generic website", base_url=base)
    return SourcePlatform.WEBSITE


async def _is_shopify(base: str, client: httpx.AsyncClient) -> bool:
    meta = await _probe(client, f"{base}{META_PATH}")
    if meta is not None and isinstance(meta, dict) and "myshopify_domain" in meta:
        return True
    payload = await _probe(client, f"{base}{PRODUCTS_PATH}", params={"limit": "1"})
    return isinstance(payload, dict) and isinstance(payload.get("products"), list)


async def _is_woocommerce(base: str, client: httpx.AsyncClient) -> bool:
    for path in STORE_API_PATHS:
        payload = await _probe(client, f"{base}{path}", params={"per_page": "1"})
        if isinstance(payload, list):
            return True
    return False


async def _probe(
    client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None
) -> object | None:
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError:
        return None
    if response.status_code != httpx.codes.OK:
        return None
    if "json" not in response.headers.get("content-type", "").lower():
        return None
    try:
        payload: object = response.json()
    except ValueError:
        return None
    return payload
