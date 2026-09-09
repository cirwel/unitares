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

Ground truth here is defined by *reference* into a store that keeps mutating.
Labeled rows are ordinary discoveries, and lifecycle policy archives them. A
target that has left the scope being measured cannot be retrieved, so a miss on
it is lifecycle, not ranking.

Measured 2026-09-09, this set is **more than half decayed**: 12 of its 21
labeled rows sit outside the default scope, and 8 of 22 queries have no
reachable target left. Treat the seed corpus as expired, not as a baseline.

The eval reports this and **changes no score**:

- `aggregate.labels_out_of_scope` / `labels_undetermined` / `labels_total`
  ride inside `aggregate` because the weekly gate logs that dict and nothing
  else. Anything reported outside it reaches no reader that exists.
- `label_health` carries the per-id detail; `per_query[].out_of_scope_ids`
  names the decayed targets for one query.
- `--include-archived` / `--include-cold` widen the measured scope, on the
  search as well as the classification. **Run the gate with both** if you want
  a constant denominator across time.

Every labeled target stays in the denominator whether it has decayed or not.
An earlier version of this file excluded decayed queries from the aggregate;
that was wrong and was caught in review. On the gate's 5-query sample it would
have cut n from 5 to 3 and left every survivor with a single target, so recall
and nDCG would have **risen** with no change to the ranker, appended to the
same series file. Trading a false regression for a false improvement is not a
fix.

Two boundaries worth knowing:

- Only `archived` and `cold` count as out of scope, mirroring the serving
  predicate, which is a denylist. A status nobody anticipated is never called
  decay. Maintaining a parallel allowlist of "reachable" statuses is what the
  first cut did, and it silently dropped rows that search would have returned.
- A label that is `missing`, `unknown`, or whose lookup failed is
  **undetermined**, never decay. Those still count as relevant, so a run
  against the wrong database scores a loud zero rather than a quiet clean
  sheet. Zero scored queries reports "no metrics available" and a null
  `flat_miss_rate`, never `0.0`.

**Do not quote a point score from this corpus as a fixed property.** The
collection grows under it: the same widened-scope run moved from nDCG@10 0.608
to 0.586 inside one session as rows were added. Cite the corpus size and date
alongside any number, or cite nothing.

The pinned `baseline_2026-04-20*.json` files are **not comparable** to current
runs and have not been since the corpus grew past their `pair_count: 20`. That
break predates the scope work. Do not diff against them without re-pinning.

## Growing the corpus

The seed set (22 pairs) is proof-of-life, not gold. Target is 100+
pairs over time. Add pairs whenever:

- An agent writes a `dogfood` or `design-gap` note complaining about search (we want the complaint resolvable by retrieval).
- A user issues a query that should have worked and didn't.
- New load-bearing discoveries land (paper-v6 claims, architecture decisions).

Labels should reflect **real agent query patterns**, not synthetic rewordings of
document titles — the latter overfits to embedding cosine on-surface.
