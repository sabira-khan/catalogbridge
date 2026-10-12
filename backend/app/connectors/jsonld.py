from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

SCRIPT_TYPE = "application/ld+json"
PRODUCT_TYPES = frozenset({"product", "productmodel", "individualproduct"})
LIST_KEYS = ("@graph", "itemListElement", "mainEntity", "hasVariant")
JSON_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        values = {key.lower(): (value or "").lower() for key, value in attrs}
        if values.get("type", "").strip() == SCRIPT_TYPE:
            self._capturing = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self.blocks.append("".join(self._buffer))
            self._capturing = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)


def extract_jsonld_blocks(html: str) -> list[object]:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    parsed: list[object] = []
    for block in collector.blocks:
        cleaned = JSON_COMMENT.sub("", block).strip()
        if not cleaned:
            continue
        try:
            parsed.append(json.loads(cleaned))
        except json.JSONDecodeError:
            continue
    return parsed


def find_products(payload: object, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(find_products(item, depth + 1))
        return found
    if not isinstance(payload, dict):
        return found
    if _is_product(payload):
        found.append(payload)
    for key in LIST_KEYS:
        if key in payload:
            found.extend(find_products(payload[key], depth + 1))
    if "item" in payload:
        found.extend(find_products(payload["item"], depth + 1))
    return found


def _is_product(payload: dict[str, Any]) -> bool:
    raw_type = payload.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(isinstance(t, str) and t.strip().lower() in PRODUCT_TYPES for t in types)
