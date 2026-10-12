# Evaluation results

Target: **Meridian Marketplace (Apparel)** (`meridian_apparel`), 30 categories
Generated: 2026-10-12 08:56 UTC

## Retrieval

Does the correct category reach the shortlist at all? This is the ceiling on
everything the model can do afterwards.

| Retriever | k | Products | Recall@k | Top-1 | No candidate | Mean rank |
|---|---|---|---|---|---|---|
| keyword | 5 | 104 | 94.2% | 76.0% | 4.8% | 1.36 |

## Mapping

Top-1 is the baseline to beat: it is what you get by picking the highest
scoring retrieval result with no model at all.

| Retriever | Model | Products | Accuracy | Unmapped | Attributes | Review rate |
|---|---|---|---|---|---|---|
| _not run_ | | | | | | |

Mapping needs a Nebius Token Factory key. Retrieval above ran without one.
