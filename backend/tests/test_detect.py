from __future__ import annotations

import pathlib

import httpx
import respx
from app.connectors.detect import detect_local, detect_source
from app.connectors.registry import build_client
from app.models.enums import SourcePlatform

from tests.conftest import SettingsFactory

SITE = "https://examplestore.com"


def test_csv_path_is_detected_locally(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text("title\nAlpha\n", encoding="utf-8")
    assert detect_local(str(path)) is SourcePlatform.CSV


def test_xlsx_path_is_detected_locally(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "catalog.xlsx"
    path.write_bytes(b"not really xlsx")
    assert detect_local(str(path)) is SourcePlatform.XLSX


def test_a_url_is_never_detected_as_local() -> None:
    assert detect_local(SITE) is None


def test_a_missing_path_is_not_detected_as_local(tmp_path: pathlib.Path) -> None:
    assert detect_local(str(tmp_path / "absent.csv")) is None


def test_an_unsupported_local_file_is_not_detected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "catalog.pdf"
    path.write_text("x", encoding="utf-8")
    assert detect_local(str(path)) is None


@respx.mock
async def test_shopify_is_detected_from_meta(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/meta.json").mock(
        return_value=httpx.Response(200, json={"myshopify_domain": "x.myshopify.com"})
    )
    settings = settings_factory()
    async with build_client(settings) as client:
        assert await detect_source(SITE, settings, client) is SourcePlatform.SHOPIFY


@respx.mock
async def test_shopify_is_detected_from_products_json(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(f"{SITE}/meta.json").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{SITE}/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
    settings = settings_factory()
    async with build_client(settings) as client:
        assert await detect_source(SITE, settings, client) is SourcePlatform.SHOPIFY


@respx.mock
async def test_woocommerce_is_detected(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/meta.json").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{SITE}/products.json").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{SITE}/wp-json/wc/store/v1/products").mock(
        return_value=httpx.Response(200, json=[])
    )
    settings = settings_factory()
    async with build_client(settings) as client:
        assert await detect_source(SITE, settings, client) is SourcePlatform.WOOCOMMERCE


@respx.mock
async def test_an_unknown_site_falls_back_to_website(settings_factory: SettingsFactory) -> None:
    respx.get(f"{SITE}/meta.json").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{SITE}/products.json").mock(return_value=httpx.Response(200, html="<html/>"))
    respx.get(f"{SITE}/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/wp-json/wc/store/products").mock(return_value=httpx.Response(404))
    settings = settings_factory()
    async with build_client(settings) as client:
        assert await detect_source(SITE, settings, client) is SourcePlatform.WEBSITE


@respx.mock
async def test_a_shopify_shaped_object_without_products_is_not_shopify(
    settings_factory: SettingsFactory,
) -> None:
    respx.get(f"{SITE}/meta.json").mock(return_value=httpx.Response(200, json={"name": "x"}))
    respx.get(f"{SITE}/products.json").mock(return_value=httpx.Response(200, json={"errors": "no"}))
    respx.get(f"{SITE}/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/wp-json/wc/store/products").mock(return_value=httpx.Response(404))
    settings = settings_factory()
    async with build_client(settings) as client:
        assert await detect_source(SITE, settings, client) is SourcePlatform.WEBSITE
