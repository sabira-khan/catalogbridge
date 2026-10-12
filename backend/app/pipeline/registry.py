from __future__ import annotations

from app.errors import UnsupportedSourceError
from app.models.enums import SourcePlatform
from app.pipeline.base import Normalizer
from app.pipeline.normalize import ShopifyNormalizer
from app.pipeline.woocommerce import WooCommerceNormalizer

NORMALIZERS: dict[SourcePlatform, type[Normalizer]] = {
    SourcePlatform.SHOPIFY: ShopifyNormalizer,
    SourcePlatform.WOOCOMMERCE: WooCommerceNormalizer,
}


def get_normalizer(platform: SourcePlatform) -> Normalizer:
    try:
        normalizer_class = NORMALIZERS[platform]
    except KeyError as exc:
        raise UnsupportedSourceError(f"no normalizer for platform {platform}") from exc
    return normalizer_class()
