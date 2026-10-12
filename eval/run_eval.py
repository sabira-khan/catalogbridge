from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.eval.dataset import RESULTS_FILE, load_dataset
from app.eval.runner import run_mapping, run_retrieval, write_results
from app.pipeline.candidates import DEFAULT_TOP_K
from app.pipeline.targets import find_target_spec

TARGET_ID = "meridian_apparel"


async def main() -> int:
    spec = find_target_spec(TARGET_ID)
    if spec is None:
        print(f"no target spec with id {TARGET_ID}", file=sys.stderr)
        return 1

    settings = get_settings()
    labelled, total = load_dataset().coverage()
    if not labelled:
        print("no labelled products, run: catalogbridge eval bootstrap", file=sys.stderr)
        return 1

    print(f"{labelled} of {total} products are labelled")
    retrieval = [await run_retrieval(spec, settings, "keyword", DEFAULT_TOP_K)]
    mapping = []
    if settings.llm_configured:
        retrieval.append(await run_retrieval(spec, settings, "embedding", DEFAULT_TOP_K))
        mapping.append(await run_mapping(spec, settings, "embedding", DEFAULT_TOP_K))
    else:
        print("no NEBIUS_API_KEY, running retrieval only")

    write_results(RESULTS_FILE, spec, retrieval, mapping)
    print(f"wrote {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
