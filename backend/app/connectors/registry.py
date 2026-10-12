from __future__ import annotations

import httpx

from app.config import Settings
from app.connectors.base import Connector
from app.connectors.shopify import ShopifyConnector
from app.connectors.spreadsheet import SpreadsheetConnector
from app.connectors.website import WebsiteConnector
from app.connectors.woocommerce import WooCommerceConnector
from app.errors import UnsupportedSourceError
from app.models.enums import SourcePlatform

CONNECTORS: dict[SourcePlatform, type[Connector]] = {
    SourcePlatform.SHOPIFY: ShopifyConnector,
    SourcePlatform.WOOCOMMERCE: WooCommerceConnector,
    SourcePlatform.CSV: SpreadsheetConnector,
    SourcePlatform.XLSX: SpreadsheetConnector,
    SourcePlatform.WEBSITE: WebsiteConnector,
}


def get_connector(
    platform: SourcePlatform,
    source: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> Connector:
    try:
        connector_class = CONNECTORS[platform]
    except KeyError as exc:
        raise UnsupportedSourceError(f"no connector for platform {platform}") from exc
    return connector_class(source, settings, client)


def build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.http_user_agent, "Accept": "application/json"},
    )
