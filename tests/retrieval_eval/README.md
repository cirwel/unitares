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

## Label decay

Labeled rows are ordinary discoveries, and lifecycle policy keeps archiving
them. A target that has left the scope being measured cannot be retrieved by
definition, so scoring it as a ranking miss manufactures a regression out of
routine housekeeping.

Measured 2026-09-09, this set is more than half decayed: 12 of its 21 labeled
rows have left the default scope, and 8 of 22 queries have no reachable target
left. `com.unitares.kg-corpus-rerank-gate` samples the first 5 queries, two of
which lost their only target to archival on 2026-09-08; its recall fell 0.567
to 0.167 with no change to the retrieval stack. Over all 22 pairs with the
scope widened, the same corpus gives recall 0.795 and nDCG 0.608.

The eval therefore reports `label_health` and refuses to score a query whose
targets have all left scope:

- `label_health.out_of_scope` names every decayed label and its status.
- A fully decayed query moves to `unscorable` and leaves the aggregate. It is
  not a flat miss.
- A partly decayed query scores against its reachable targets only.
- `--include-archived` / `--include-cold` widen the measured scope, on both the
  search and the classification, when you want the full labeled corpus back.

Read `label_health` before `aggregate`. A rising `out_of_scope_count` is the
label set decaying, and that is indistinguishable from a quality drop in the
metrics alone. A status lookup that fails is treated as reachable, so a broken
backend can never quietly shrink the scored set.

## Growing the corpus

The seed set (22 pairs) is proof-of-life, not gold. Target is 100+
pairs over time. Add pairs whenever:

- An agent writes a `dogfood` or `design-gap` note complaining about search (we want the complaint resolvable by retrieval).
- A user issues a query that should have worked and didn't.
- New load-bearing discoveries land (paper-v6 claims, architecture decisions).

Labels should reflect **real agent query patterns**, not synthetic rewordings of
document titles — the latter overfits to embedding cosine on-surface.
