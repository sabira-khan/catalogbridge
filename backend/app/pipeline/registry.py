from __future__ import annotations

from app.errors import UnsupportedSourceError
from app.llm.client import LLMClient
from app.models.enums import SourcePlatform
from app.pipeline.base import Normalizer
from app.pipeline.normalize import ShopifyNormalizer
from app.pipeline.spreadsheet import SpreadsheetNormalizer
from app.pipeline.website import WebsiteNormalizer
from app.pipeline.woocommerce import WooCommerceNormalizer

NORMALIZERS: dict[SourcePlatform, type[Normalizer]] = {
    SourcePlatform.SHOPIFY: ShopifyNormalizer,
    SourcePlatform.WOOCOMMERCE: WooCommerceNormalizer,
    SourcePlatform.WEBSITE: WebsiteNormalizer,
    SourcePlatform.CSV: SpreadsheetNormalizer,
    SourcePlatform.XLSX: SpreadsheetNormalizer,
}


def get_normalizer(platform: SourcePlatform, llm: LLMClient | None = None) -> Normalizer:
    try:
        normalizer_class = NORMALIZERS[platform]
    except KeyError as exc:
        raise UnsupportedSourceError(f"no normalizer for platform {platform}") from exc
    return normalizer_class(llm)
