# CatalogBridge

Migrates a vendor's own product catalog from any source (Shopify, WooCommerce, website,
spreadsheet) into any target marketplace schema, with multilingual output and human review.

Built for the Nebius x NVIDIA Global AI Hackathon 2026. All model inference runs on Nebius Token
Factory. NVIDIA Nemotron does the normalization and category mapping, the two stages that carry the
pipeline, and every call is attributed by stage and model so its role is visible rather than
asserted.

## Why this exists

A vendor who wants to sell on a new marketplace has to restate their whole catalog in that
marketplace's schema: its category tree, its required attributes, its language. Doing that by hand
is the reason small sellers stay on one channel. The hard part is not moving the data, it is
deciding what each product actually is.

## Status

Milestone 1 is complete:

- Canonical product schema as Pydantic v2 models
- Shopify connector over the public `/products.json` feed, with pagination, retries and typed errors
- Deterministic Shopify to canonical normalizer
- Nemotron client wrapper with strict JSON schema output, validation and a repair round trip
- A CLI that prints normalized canonical products from a store URL

Everything that does not need a Nebius account is built:

- Four sources: Shopify, WooCommerce, generic websites with JSON-LD, and CSV or XLSX files, with
  the platform detected automatically
- Target marketplaces as YAML specs, with JSON and CSV exporters
- Category mapping: embedding retrieval, then Nemotron picking one with a confidence and a reason
- A review UI in Nuxt, where corrections are fed back as examples for the rest of the batch
- An evaluation harness comparing embeddings plus Nemotron against a keyword baseline

Still to come: translation, Shopify draft export, and the evaluation numbers themselves, which
need a Token Factory key.

## Setup

Requires [uv](https://docs.astral.sh/uv/) for the backend and Node 20 or newer for the UI.

```sh
uv python install 3.12
uv sync
cp .env.example .env
```

Extraction and normalization need no API key. Only the LLM stages do. To use them, put a Nebius
Token Factory key in `.env` as `NEBIUS_API_KEY`.

For the review UI:

```sh
cd frontend
npm install
npm run dev
```

It expects the API at `http://127.0.0.1:8000`. Override with `NUXT_PUBLIC_API_BASE`, and add the
UI's origin to `CORS_ORIGINS` in `.env` if you change its port.

```sh
uv run uvicorn app.main:app --reload
```

## Usage

Print a normalized catalog as a table:

```sh
uv run catalogbridge extract deathwishcoffee.com --i-own-this-catalog
```

```
                                     Death Wish Coffee (3 products)
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃   # ┃ Title                        ┃ Brand            ┃ Category         ┃       Price ┃ Vars ┃ Attrs ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│   1 │ Gingerdead Single-Serve Pods │ Death Wish Coff… │ Coffee           │   12.99 USD │    3 │     0 │
│   2 │ Gingerdead Coffee            │ Death Wish Coff… │ Coffee           │   13.99 USD │    3 │     0 │
│   3 │ Crossbones Klean Kanteen Mug │ Klean Kanteen    │ On-The-Go Drink… │   25.00 USD │    1 │     1 │
└─────┴──────────────────────────────┴──────────────────┴──────────────────┴─────────────┴──────┴───────┘
1 page fetched from https://deathwishcoffee.com, currency USD
```

Export the full canonical records:

```sh
uv run catalogbridge extract deathwishcoffee.com --format json --out products.json --i-own-this-catalog
uv run catalogbridge extract deathwishcoffee.com --format jsonl --limit 50 --i-own-this-catalog
```

Map a catalog into a target marketplace and export it:

```sh
uv run catalogbridge map allbirds.com --target meridian_apparel --format csv --out mapped.csv --i-own-this-catalog
```

See which categories retrieval shortlists, with no model calls and no API key at all:

```sh
uv run catalogbridge map allbirds.com --target meridian_apparel \
  --candidates-only --retriever keyword --i-own-this-catalog
```

Inspect the target marketplaces you can migrate into:

```sh
uv run catalogbridge targets
uv run catalogbridge targets --id meridian_apparel
```

Confirm your Token Factory key and model work:

```sh
uv run catalogbridge llm-check
```

`--i-own-this-catalog` is required. Without it the CLI asks for confirmation before making any
request, because you should only migrate a catalog you own or are authorized to migrate.

## Architecture

Sources and targets are both pluggable. Adding a source means one `Connector`, one `Normalizer` and
two registry entries, with no changes anywhere else.

| Stage | Module | Nemotron? |
|---|---|---|
| Extract | `backend/app/connectors` | No |
| Normalize | `backend/app/pipeline/normalize.py` | Only for unstructured sources |
| Map | `backend/app/pipeline` | Yes, category selection with confidence |
| Translate | `backend/app/pipeline` | Yes |
| Load | `backend/app/pipeline/load.py` | No |

A target marketplace is a YAML file in `targets/`, holding its category tree, its globally and
per-category required attributes, its supported languages and its review threshold. Adding a target
means adding a file, not changing code. `targets/meridian_apparel.yaml` is an invented apparel
marketplace with 20 categories, used for the demo and the evaluation. It is not any real company's
schema.

Structured sources such as Shopify take a deterministic path, because an LLM adds cost and risk
where the data is already clean. Unstructured sources, spreadsheets and web pages, go through
Nemotron into the same canonical schema. `Normalizer.requires_llm` marks which is which.

## Evaluation

```sh
uv run catalogbridge eval bootstrap --store allbirds.com --target meridian_apparel --per-store 26 --i-own-this-catalog
uv run catalogbridge eval run --target meridian_apparel
```

See `eval/README.md` for the method, including what it deliberately does not do.

## Tests

```sh
uv run pytest
uv run ruff check .
uv run mypy backend/app
cd frontend && npx nuxt typecheck
```

No test touches the network. Shopify fixtures are trimmed captures of real public product feeds.
WooCommerce fixtures are written to the documented Store API schema, not captured, because no
public store exposing that API could be found. Shopify fixtures are trimmed captures of real public product feeds.

## License

MIT
