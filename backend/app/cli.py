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
from app.eval.dataset import LABELS_FILE, PRODUCTS_FILE, RESULTS_FILE, load_dataset
from app.eval.runner import (
    bootstrap_labels,
    collect_products,
    run_mapping,
    run_retrieval,
    write_results,
)
from app.llm.client import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.llm.prompts.loader import render_prompt
from app.llm.usage import UsageRecord
from app.logging import configure_logging
from app.models.canonical import Product
from app.models.enums import SourcePlatform
from app.models.llm_io import ConnectivityCheck
from app.models.mapping import CategoryCandidate, MappedProduct
from app.models.target import TargetSpec
from app.pipeline.candidates import (
    DEFAULT_TOP_K,
    CandidateSource,
    EmbeddingCandidates,
    KeywordCandidates,
)
from app.pipeline.load import LOADERS, get_loader
from app.pipeline.map import CategoryMapper
from app.pipeline.registry import get_normalizer
from app.pipeline.targets import find_target_spec, load_target_specs

OWNERSHIP_NOTICE = (
    "CatalogBridge reads a store's public product feed. Only run it against a catalog "
    "you own or are authorized to migrate."
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Migrate a vendor product catalog into a target marketplace schema.",
)
eval_app = typer.Typer(no_args_is_help=True, help="Measure mapping quality against labels.")
app.add_typer(eval_app, name="eval")

console = Console()
errors = Console(stderr=True)


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    JSONL = "jsonl"


class Retriever(StrEnum):
    KEYWORD = "keyword"
    EMBEDDING = "embedding"


class MapFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


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


@app.command("map")
def map_catalog(
    source: Annotated[str, typer.Argument(help="Store URL, for example examplestore.com")],
    target: Annotated[str, typer.Option("--target", help="Target spec id")],
    platform: Annotated[
        SourcePlatform, typer.Option("--platform", help="Source platform")
    ] = SourcePlatform.SHOPIFY,
    retriever: Annotated[
        Retriever, typer.Option("--retriever", help="How candidates are retrieved")
    ] = Retriever.EMBEDDING,
    top_k: Annotated[int, typer.Option("--top-k", help="Candidates per product")] = DEFAULT_TOP_K,
    candidates_only: Annotated[
        bool,
        typer.Option(
            "--candidates-only",
            help="Stop after retrieval and show candidates, no model calls",
        ),
    ] = False,
    map_format: Annotated[
        MapFormat, typer.Option("--format", help="How to print the result")
    ] = MapFormat.TABLE,
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after N products")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write to a file")] = None,
    i_own_this_catalog: Annotated[
        bool, typer.Option("--i-own-this-catalog", help="Confirm you may migrate this catalog")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Log every step")] = False,
) -> None:
    settings = _settings(verbose)
    spec = find_target_spec(target)
    if spec is None:
        errors.print(f"[red]No target spec with id {target}. Try: catalogbridge targets[/red]")
        raise typer.Exit(1)
    needs_key = not candidates_only or retriever is Retriever.EMBEDDING
    if needs_key and not settings.llm_configured:
        errors.print("[red]NEBIUS_API_KEY is not set. Copy .env.example to .env first.[/red]")
        errors.print(
            "[yellow]To see retrieval without a key: "
            "--candidates-only --retriever keyword[/yellow]"
        )
        raise typer.Exit(1)
    if not i_own_this_catalog:
        errors.print(f"[yellow]{OWNERSHIP_NOTICE}[/yellow]")
        typer.confirm("Do you own or have authorization to migrate this catalog?", abort=True)

    try:
        products, context = asyncio.run(_extract(source, platform, settings, limit))
        if candidates_only:
            shortlists = asyncio.run(_shortlists(products, spec, settings, retriever, top_k))
            _print_candidates(shortlists, spec.id)
            return
        mapped = asyncio.run(_map(products, spec, settings, retriever, top_k))
    except CatalogBridgeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if map_format is MapFormat.TABLE and out is None:
        _print_mapped(mapped, spec)
        return
    rendered = get_loader("csv" if map_format is MapFormat.CSV else "json").dump(mapped, spec)
    if out is not None:
        out.write_text(rendered, encoding="utf-8")
        console.print(f"Wrote {len(mapped)} mapped products to {out}")
    else:
        console.print(rendered, soft_wrap=True)


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


@eval_app.command("bootstrap")
def eval_bootstrap(
    store: Annotated[
        list[str], typer.Option("--store", help="Shopify store URL, repeat for more")
    ],
    target: Annotated[str, typer.Option("--target", help="Target spec id")],
    per_store: Annotated[
        int, typer.Option("--per-store", help="Products to take from each store")
    ] = 30,
    i_own_this_catalog: Annotated[
        bool, typer.Option("--i-own-this-catalog", help="Confirm you may read these catalogs")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    settings = _settings(verbose)
    spec = find_target_spec(target)
    if spec is None:
        errors.print(f"[red]No target spec with id {target}[/red]")
        raise typer.Exit(1)
    if not i_own_this_catalog:
        errors.print(f"[yellow]{OWNERSHIP_NOTICE}[/yellow]")
        typer.confirm("Do you own or have authorization to read these catalogs?", abort=True)

    try:
        pairs = asyncio.run(collect_products(list(store), settings, per_store))
        rows = asyncio.run(bootstrap_labels(pairs, spec, PRODUCTS_FILE, LABELS_FILE))
    except CatalogBridgeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    done = sum(1 for row in rows if row.labelled)
    console.print(f"Cached {len(pairs)} products to {PRODUCTS_FILE}")
    console.print(f"Wrote {len(rows)} label rows to {LABELS_FILE}")
    console.print(
        f"[bold]{done} of {len(rows)} labelled.[/bold] Fill expected_category_id for the rest, "
        "then run: catalogbridge eval run --target " + target
    )


@eval_app.command("run")
def eval_run(
    target: Annotated[str, typer.Option("--target", help="Target spec id")],
    top_k: Annotated[int, typer.Option("--top-k", help="Candidates per product")] = DEFAULT_TOP_K,
    with_mapping: Annotated[
        bool, typer.Option("--with-mapping/--retrieval-only", help="Also run the model")
    ] = True,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    settings = _settings(verbose)
    spec = find_target_spec(target)
    if spec is None:
        errors.print(f"[red]No target spec with id {target}[/red]")
        raise typer.Exit(1)

    dataset = load_dataset()
    labelled, total = dataset.coverage()
    if not labelled:
        errors.print(
            f"[red]No labelled products. Fill expected_category_id in {LABELS_FILE} first.[/red]"
        )
        raise typer.Exit(1)
    console.print(f"{labelled} of {total} products are labelled")

    retrieval = [asyncio.run(run_retrieval(spec, settings, "keyword", top_k))]
    mapping = []
    if settings.llm_configured:
        retrieval.append(asyncio.run(run_retrieval(spec, settings, "embedding", top_k)))
        if with_mapping:
            mapping.append(asyncio.run(run_mapping(spec, settings, "embedding", top_k)))
    else:
        console.print(
            "[yellow]No NEBIUS_API_KEY, so embedding retrieval and mapping were skipped.[/yellow]"
        )

    table = Table(title="Retrieval")
    table.add_column("Retriever")
    table.add_column("Recall@k", justify="right")
    table.add_column("Top-1", justify="right")
    table.add_column("No candidate", justify="right")
    table.add_column("Mean rank", justify="right")
    for report in retrieval:
        table.add_row(
            report.retriever,
            f"{report.recall_at_k * 100:.1f}%",
            f"{report.top1_accuracy * 100:.1f}%",
            f"{report.no_candidate_rate * 100:.1f}%",
            "-" if report.mean_rank_of_correct is None else str(report.mean_rank_of_correct),
        )
    console.print(table)
    for item in mapping:
        console.print(
            f"Mapping accuracy {item.accuracy * 100:.1f}%, attributes "
            f"{item.attribute_completeness * 100:.1f}%, review rate "
            f"{item.needs_review_rate * 100:.1f}%"
        )

    write_results(RESULTS_FILE, spec, retrieval, mapping)
    console.print(f"Wrote {RESULTS_FILE}")


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


def _candidate_source(
    settings: Settings, retriever: Retriever
) -> CandidateSource:
    if retriever is Retriever.KEYWORD:
        return KeywordCandidates()
    return EmbeddingCandidates(EmbeddingClient(settings))


async def _shortlists(
    products: list[Product],
    spec: TargetSpec,
    settings: Settings,
    retriever: Retriever,
    top_k: int,
) -> list[tuple[Product, list[CategoryCandidate]]]:
    source = _candidate_source(settings, retriever)
    return [(p, await source.candidates(p, spec, top_k)) for p in products]


async def _map(
    products: list[Product],
    spec: TargetSpec,
    settings: Settings,
    retriever: Retriever,
    top_k: int,
) -> list[MappedProduct]:
    mapper = CategoryMapper(
        spec, LLMClient(settings), _candidate_source(settings, retriever), top_k=top_k
    )
    return await mapper.map_products(products)


def _print_candidates(
    shortlists: list[tuple[Product, list[CategoryCandidate]]], target_id: str
) -> None:
    table = Table(title=f"Retrieved candidates for {target_id}")
    table.add_column("Product", max_width=34, no_wrap=True, overflow="ellipsis")
    table.add_column("Top candidates")
    for product, candidates in shortlists:
        joined = ", ".join(f"{c.category_id} {c.similarity:.2f}" for c in candidates)
        rendered = joined or "[dim]none[/dim]"
        table.add_row(product.title, rendered)
    console.print(table)
    empty = sum(1 for _, candidates in shortlists if not candidates)
    console.print(
        f"{len(shortlists)} products, {empty} with no candidate. No model calls were made."
    )


def _print_mapped(mapped: list[MappedProduct], spec: TargetSpec) -> None:
    table = Table(title=f"{spec.name}: {len(mapped)} products")
    table.add_column("Product", max_width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("Category", max_width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Conf", justify="right", width=5)
    table.add_column("Missing", max_width=20, overflow="ellipsis")
    table.add_column("Review", width=6)
    for item in mapped:
        table.add_row(
            item.product.title,
            " > ".join(item.target_category_path) or "[red]unmapped[/red]",
            f"{item.category_confidence:.2f}",
            ", ".join(item.missing_attributes) or "-",
            "[yellow]yes[/yellow]" if item.needs_review else "no",
        )
    console.print(table)
    flagged = sum(1 for item in mapped if item.needs_review)
    unmapped = sum(1 for item in mapped if item.target_category_id is None)
    confidences = [item.category_confidence for item in mapped]
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    console.print(
        f"{flagged} of {len(mapped)} need review, {unmapped} unmapped, "
        f"mean confidence {mean:.2f}, threshold {spec.confidence_threshold:.2f}"
    )


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
