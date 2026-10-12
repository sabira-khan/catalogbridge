from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from app.config import Settings
from app.errors import ConnectorHTTPError, ShopifyAdminError
from app.logging import get_logger, log_event
from app.models.mapping import MappedProduct
from app.models.target import TargetSpec

ADMIN_VERSION = "2026-01"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_OPTIONS = 3

logger = get_logger(__name__)


class ShopifyDraftLoader:
    format = "shopify_draft"

    def __init__(
        self,
        shop_domain: str,
        access_token: str,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        if not access_token:
            raise ShopifyAdminError("a Shopify Admin API access token is required")
        self.shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
        self.access_token = access_token
        self.settings = settings
        self.client = client

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{ADMIN_VERSION}/products.json"

    async def push(
        self, products: list[MappedProduct], spec: TargetSpec, *, skip_flagged: bool = True
    ) -> dict[str, Any]:
        created: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []

        for mapped in products:
            if skip_flagged and mapped.needs_review:
                skipped.append(mapped.product.title)
                continue
            try:
                product_id = await self._create(mapped, spec)
            except ShopifyAdminError as exc:
                failed.append({"title": mapped.product.title, "error": str(exc)})
                continue
            created.append(product_id)
            if self.settings.connector_page_delay > 0:
                await asyncio.sleep(self.settings.connector_page_delay)

        log_event(
            logger,
            "pushed drafts to Shopify",
            created=len(created),
            skipped=len(skipped),
            failed=len(failed),
        )
        return {
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "endpoint": self.endpoint,
        }

    async def _create(self, mapped: MappedProduct, spec: TargetSpec) -> str:
        response = await self._post(self.endpoint, {"product": draft_payload(mapped, spec)})
        try:
            payload = response.json()
        except ValueError as exc:
            raise ShopifyAdminError("Shopify returned a non-JSON response") from exc
        product = payload.get("product") if isinstance(payload, dict) else None
        if not isinstance(product, dict) or "id" not in product:
            raise ShopifyAdminError(f"unexpected Shopify response: {payload}")
        return str(product["id"])

    async def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        attempts = self.settings.connector_max_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.post(
                    url,
                    json=body,
                    headers={
                        "X-Shopify-Access-Token": self.access_token,
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                await self._backoff(attempt, None)
                continue
            if response.status_code in RETRYABLE_STATUS:
                if attempt == attempts:
                    raise ShopifyAdminError(f"Shopify kept returning {response.status_code}")
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code == httpx.codes.UNAUTHORIZED:
                raise ShopifyAdminError("Shopify rejected the access token")
            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise ShopifyAdminError(
                    f"Shopify returned {response.status_code}: {response.text[:200]}"
                )
            return response
        raise ConnectorHTTPError(url, 0, str(last_error))

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.settings.connector_backoff_base * (2 ** (attempt - 1))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, self.settings.connector_backoff_cap))


def draft_payload(mapped: MappedProduct, spec: TargetSpec) -> dict[str, Any]:
    product = mapped.product
    option_names = _option_names(mapped)
    payload: dict[str, Any] = {
        "title": product.title,
        "body_html": _body_html(product.description),
        "vendor": product.brand or "",
        "product_type": " > ".join(mapped.target_category_path),
        "status": "draft",
        "published": False,
        "tags": _tags(mapped, spec),
        "variants": _variants(mapped, option_names),
        "images": [{"src": image.url} for image in product.images],
    }
    if option_names:
        payload["options"] = [{"name": name} for name in option_names]
    metafields = _metafields(mapped)
    if metafields:
        payload["metafields"] = metafields
    return payload


def _body_html(description: str | None) -> str:
    if not description:
        return ""
    paragraphs = [block.strip() for block in description.split("\n\n") if block.strip()]
    return "".join(f"<p>{_escape(block)}</p>" for block in paragraphs)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def _option_names(mapped: MappedProduct) -> list[str]:
    names: list[str] = []
    for variant in mapped.product.variants:
        for option in variant.options:
            if option.name not in names:
                names.append(option.name)
    return names[:MAX_OPTIONS]


def _variants(mapped: MappedProduct, option_names: list[str]) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for variant in mapped.product.variants:
        values = {option.name: option.value for option in variant.options}
        entry: dict[str, Any] = {
            "price": str(variant.price) if variant.price is not None else "0.00",
            "inventory_management": None,
        }
        if variant.sku:
            entry["sku"] = variant.sku
        if variant.compare_at_price is not None:
            entry["compare_at_price"] = str(variant.compare_at_price)
        for index, name in enumerate(option_names, start=1):
            if name in values:
                entry[f"option{index}"] = values[name]
        variants.append(entry)
    return variants or [{"price": "0.00", "inventory_management": None}]


def _tags(mapped: MappedProduct, spec: TargetSpec) -> str:
    tags = list(mapped.product.tags)
    tags.append(f"catalogbridge:{spec.id}")
    if mapped.needs_review:
        tags.append("catalogbridge:needs-review")
    if mapped.target_category_id:
        tags.append(f"catalogbridge:{mapped.target_category_id}")
    return ", ".join(dict.fromkeys(tags))


def _metafields(mapped: MappedProduct) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for attribute in mapped.mapped_attributes:
        value = attribute.value
        if attribute.unit:
            value = f"{value} {attribute.unit}"
        fields.append(
            {
                "namespace": "catalogbridge",
                "key": attribute.target_key[:30],
                "value": value,
                "type": "single_line_text_field",
            }
        )
    return fields
