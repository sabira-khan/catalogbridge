# CatalogBridge

Migrates a vendor's own product catalog from any source (Shopify, WooCommerce, website, spreadsheet)
into any target marketplace schema, with multilingual output and human review.

Built for the Nebius x NVIDIA Global AI Hackathon 2026. All model inference runs on Nebius Token
Factory. NVIDIA Nemotron does the normalization and category mapping, the two stages that carry the
pipeline.

## Status

Milestone 1 in progress: backend scaffold, canonical schema, Shopify connector, LLM client wrapper,
and a CLI that prints normalized canonical products from a Shopify store URL.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync
cp .env.example .env
```

Put your Nebius Token Factory API key in `.env` as `NEBIUS_API_KEY`. Extraction and normalization
work without a key. Only the LLM stages need one.

## Usage

```sh
uv run catalogbridge extract https://examplestore.com --i-own-this-catalog
uv run catalogbridge extract https://examplestore.com --format json --out products.json
uv run catalogbridge llm-check
```

## Tests

```sh
uv run pytest
uv run ruff check .
uv run mypy backend/app
```

No test touches the network.

## License

MIT
