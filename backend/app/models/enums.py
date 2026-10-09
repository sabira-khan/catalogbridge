from __future__ import annotations

from enum import StrEnum


class SourcePlatform(StrEnum):
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    WEBSITE = "website"
    CSV = "csv"
    XLSX = "xlsx"


class AttributeOrigin(StrEnum):
    SOURCE = "source"
    LLM = "llm"
    USER = "user"
