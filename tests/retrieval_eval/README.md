# KG Retrieval Eval Harness

Measures retrieval quality of the KG search pipeline against a labeled corpus.
Was the objective floor for the Phase 2–5 KG retrieval rebuild (shipped via
PR #55–#63, 2026-04-20; live behind `UNITARES_EMBEDDING_MODEL=bge-m3` +
`UNITARES_ENABLE_HYBRID=1`). Now serves as the regression baseline.

## Running

```bash
# Current default pipeline (semantic_search → top-K by cosine)
python scripts/eval/retrieval_eval.py

# Against a specific label file
python scripts/eval/retrieval_eval.py --labels tests/retrieval_eval/labels.json

# JSON output (for diffing across runs)
python scripts/eval/retrieval_eval.py --json > /tmp/baseline.json
```

Requires live Postgres + embeddings backend. Not part of the default `pytest` run.

## Metrics

- **nDCG@10** — primary. Captures both "is the right thing retrieved" and "is it ranked high." Ideal-DCG uses binary relevance from the labels.
- **Recall@20** — secondary. "When a relevant doc exists, does the top-20 at least contain it?" Useful ceiling for what a reranker can work with.
- **MRR** — reciprocal rank of the first hit. Cheap to compute, complements nDCG.
- **Flat-miss rate** — fraction of queries with no labeled hit anywhere in the
  fetched result set. This makes complete retrieval failures visible instead of
  hiding them inside a mean score.
- **Latency p50/p95** — per-query wallclock of the retrieval call only (not embedding the query or handler overhead).

## Label format

`tests/retrieval_eval/labels.json`:

```json
{
  "schema_version": 1,
  "pairs": [
    {
      "query": "free-text query an agent might issue",
      "relevant_ids": ["<discovery_id>", "..."],
      "rationale": "optional note on why these are the gold answers"
    }
  ]
}
```

Labels are binary (relevant / not). Order within `relevant_ids` is not significant.

## Growing the corpus

The seed set (~20 pairs, 2026-04-20) is proof-of-life, not gold. Target is 100+
pairs over time. Add pairs whenever:

- An agent writes a `dogfood` or `design-gap` note complaining about search (we want the complaint resolvable by retrieval).
- A user issues a query that should have worked and didn't.
- New load-bearing discoveries land (paper-v6 claims, architecture decisions).

Labels should reflect **real agent query patterns**, not synthetic rewordings of
document titles — the latter overfits to embedding cosine on-surface.
