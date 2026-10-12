from __future__ import annotations

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from app import __version__
from app.config import Settings, get_settings
from app.connectors.base import SourceContext
from app.connectors.registry import build_client, get_connector
from app.errors import CatalogBridgeError
from app.llm.client import LLMClient
from app.llm.prompts.loader import render_prompt
from app.llm.usage import UsageRecord
from app.logging import configure_logging
from app.models.canonical import Product
from app.models.enums import SourcePlatform
from app.models.llm_io import ConnectivityCheck
from app.models.target import TargetSpec
from app.pipeline.load import LOADERS
from app.pipeline.registry import get_normalizer
from app.pipeline.targets import load_target_specs

OWNERSHIP_NOTICE = (
    "CatalogBridge reads a store's public product feed. Only run it against a catalog "
    "you own or are authorized to migrate."
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Migrate a vendor product catalog into a target marketplace schema.",
)
console = Console()
errors = Console(stderr=True)


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    JSONL = "jsonl"


@app.command()
def extract(
    source: Annotated[str, typer.Argument(help="Store URL, for example examplestore.com")],
    platform: Annotated[
        SourcePlatform, typer.Option("--platform", help="Source platform")
    ] = SourcePlatform.SHOPIFY,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="How to print the result")
    ] = OutputFormat.TABLE,
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after N products")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages", help="Page cap")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write to a file")] = None,
    i_own_this_catalog: Annotated[
        bool, typer.Option("--i-own-this-catalog", help="Confirm you may migrate this catalog")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Log every step")] = False,
) -> None:
    settings = _settings(verbose)
    if not i_own_this_catalog:
        errors.print(f"[yellow]{OWNERSHIP_NOTICE}[/yellow]")
        typer.confirm("Do you own or have authorization to migrate this catalog?", abort=True)
    if max_pages is not None:
        settings.connector_max_pages = max_pages

    try:
        products, context = asyncio.run(_extract(source, platform, settings, limit))
    except CatalogBridgeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    rendered = _render(products, context, output_format)
    if out is not None:
        out.write_text(rendered if rendered.endswith("\n") else f"{rendered}\n", encoding="utf-8")
        console.print(f"Wrote {len(products)} products to {out}")
    elif output_format is OutputFormat.TABLE:
        _print_table(products, context)
    else:
        console.print(rendered, soft_wrap=True)


@app.command("llm-check")
def llm_check(
    model: Annotated[str | None, typer.Option("--model", help="Override LLM_MODEL")] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    settings = _settings(verbose)
    if not settings.llm_configured:
        errors.print("[red]NEBIUS_API_KEY is not set. Copy .env.example to .env first.[/red]")
        raise typer.Exit(1)
    try:
        result, usage = asyncio.run(_llm_check(settings, model))
    except CatalogBridgeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    table = Table(title="Token Factory connectivity", show_header=False)
    table.add_row("model", usage.model)
    table.add_row("model says", f"{result.model_family}: {result.one_line_summary}")
    table.add_row("structured output", usage.structured_mode)
    table.add_row("tokens", str(usage.total_tokens))
    table.add_row("latency", f"{usage.latency_ms} ms")
    table.add_row("attempts", f"{usage.attempts} ({usage.repairs} repairs)")
    console.print(table)


@app.command()
def targets(
    target_id: Annotated[
        str | None, typer.Option("--id", help="Show one target's category tree")
    ] = None,
) -> None:
    try:
        specs = load_target_specs()
    except CatalogBridgeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if not specs:
        errors.print("[yellow]No target specs found in targets/[/yellow]")
        raise typer.Exit(1)

    if target_id is None:
        table = Table(title="Target marketplaces")
        table.add_column("id")
        table.add_column("Name")
        table.add_column("Categories", justify="right")
        table.add_column("Languages")
        table.add_column("Threshold", justify="right")
        for spec in specs:
            table.add_row(
                spec.id,
                spec.name,
                str(len(spec.categories)),
                ", ".join(spec.languages),
                f"{spec.confidence_threshold:.2f}",
            )
        console.print(table)
        console.print(f"Export formats: {', '.join(sorted(LOADERS))}")
        return

    match = next((spec for spec in specs if spec.id == target_id), None)
    if match is None:
        errors.print(f"[red]No target spec with id {target_id}[/red]")
        raise typer.Exit(1)
    _print_target(match)


@app.command()
def version() -> None:
    console.print(__version__)


async def _extract(
    source: str, platform: SourcePlatform, settings: Settings, limit: int | None
) -> tuple[list[Product], SourceContext]:
    async with build_client(settings) as client:
        connector = get_connector(platform, source, settings, client)
        context = await connector.discover()
        records = []
        async for record in connector.iter_records():
            records.append(record)
            if limit is not None and len(records) >= limit:
                break
        context.pages_fetched = connector.pages_fetched
        return await get_normalizer(platform).normalize_batch(records, context), context


async def _llm_check(
    settings: Settings, model: str | None
) -> tuple[ConnectivityCheck, UsageRecord]:
    client = LLMClient(settings)
    result, usage = await client.complete_json(
        prompt=render_prompt("llm_check"),
        response_model=ConnectivityCheck,
        stage="llm_check",
        model=model,
    )
    return result, usage


def _render(products: list[Product], context: SourceContext, fmt: OutputFormat) -> str:
    payloads = [p.model_dump(mode="json", exclude={"raw"}) for p in products]
    if fmt is OutputFormat.JSONL:
        return "\n".join(json.dumps(p) for p in payloads)
    if fmt is OutputFormat.JSON:
        return json.dumps(
            {"source": context.model_dump(mode="json"), "products": payloads}, indent=2
        )
    return ""


def _print_table(products: list[Product], context: SourceContext) -> None:
    wide = console.width >= 100
    table = Table(title=f"{context.store_name or context.base_url} ({len(products)} products)")
    if wide:
        table.add_column("#", justify="right", style="dim", width=3, no_wrap=True)
    table.add_column("Title", max_width=40, no_wrap=True, overflow="ellipsis")
    if wide:
        table.add_column("Brand", max_width=16, no_wrap=True, overflow="ellipsis")
        table.add_column("Category", max_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("Price", justify="right", width=11, no_wrap=True)
    table.add_column("Vars", justify="right", width=4, no_wrap=True)
    if wide:
        table.add_column("Attrs", justify="right", width=5, no_wrap=True)

    for index, product in enumerate(products, start=1):
        row: list[str] = []
        if wide:
            row.append(str(index))
        row.append(product.title)
        if wide:
            row.append(product.brand or "-")
            row.append(product.source_category or "-")
        row.append(_price(product))
        row.append(str(len(product.variants)))
        if wide:
            row.append(str(len(product.attributes)))
        table.add_row(*row)

    console.print(table)
    pages = context.pages_fetched
    console.print(
        f"{pages} page{'' if pages == 1 else 's'} fetched from {context.base_url}, "
        f"currency {context.currency or 'unknown'}"
    )


def _print_target(spec: TargetSpec) -> None:
    console.print(f"[bold]{spec.name}[/bold] ({spec.id})")
    if spec.description:
        console.print(spec.description.strip())
    required_global = [a.key for a in spec.global_attributes if a.required]
    console.print(f"Required everywhere: {', '.join(required_global) or 'none'}")
    table = Table(title=f"{len(spec.categories)} categories")
    table.add_column("id", no_wrap=True)
    table.add_column("Path")
    table.add_column("Also required", max_width=34, overflow="ellipsis")
    for category in spec.categories:
        extra = [a.key for a in category.attributes if a.required]
        table.add_row(category.id, category.label, ", ".join(extra) or "-")
    console.print(table)


def _price(product: Product) -> str:
    if product.price is None:
        return "-"
    amount = f"{product.price:,.2f}"
    return f"{amount} {product.currency}" if product.currency else amount


def _settings(verbose: bool) -> Settings:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else "WARNING")
    return settings


if __name__ == "__main__":
    app()
