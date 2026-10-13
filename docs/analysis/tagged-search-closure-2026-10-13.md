# Tagged-search dialectic closure evidence — 2026-10-13

This report answers the measurement conditions in dialectic
`06abb3f7a18cce6f`, for dogfood finding fingerprint `bdb18ee4e52ee7d6`.
It preserves the failed historical transcript and does not claim approval by
its original reviewer. Fresh governed review: `fb00d537fd9a3678` (pending).
Classification: fix / documentation; no runtime or authorization change.

## What the original objection required

1. Before rollout, representative local/staging tagged-hybrid latency over
   multiple runs and sparse-tag recall, or an explicit operator waiver.
2. After deployment, dogfood and weak-query sparse-tag probes, with correctness
   and latency recorded against the finding and material regressions investigated.

The final implementation in [PR #1810](https://github.com/cirwel/unitares/pull/1810)
pushes tag predicates into FTS and pgvector candidate retrieval. The abandoned
candidate floor and third-arm union are not the implementation being assessed.

## Pre-rollout evidence already existed

The merged PR body records PostgreSQL 17 + AGE + pgvector, bge-m3, 1,540 rows,
and ten runs per case against master and the fix branch. These are historical
PR-reported results, not measurements rerun today against August code.

| Case | Before p50 / p95 (ms) | After p50 / p95 (ms) | Correctness after |
|---|---:|---:|---|
| Untagged | 158.9 / 240.8 | 157.8 / 166.7 | Same 14 results |
| Common tag | 171.7 / 231.6 | 154.6 / 184.9 | 16 results, zero wrong tags |
| Sparse tag, matching query | 174.5 / 226.6 | 156.3 / 194.4 | One correct result |
| Sparse tag, weak query | 166.7 / 263.2 | 140.4 / 221.8 | One correct result; previously 20 wrong |
| Nonexistent tag | 129.1 / 200.2 | 78.1 / 144.3 | Zero results; previously 20 wrong |

The [author's pre-merge comment](https://github.com/cirwel/unitares/pull/1810#issuecomment-5377669715)
records that `SELF_CLEAR_REFUSED` prevented the second synthesis from joining
this evidence to the transcript; a new review was refused with `SESSION_EXISTS`.
The original session subsequently failed on liveness. #1810 merged on
2026-08-22 at 14:19:27Z, commit `81513f3e1da13694e3716aa9cc80dde791c22b6d`.
No retrospective waiver is claimed.

## Current retrieval-stage measurements

On October 13, the existing read-only benchmark
[`scripts/ops/bench_kg_tag_filter.py`](../../scripts/ops/bench_kg_tag_filter.py)
ran ten iterations per case using the live local database and the merged
implementation (checkout `bdda576f2`, included in master `0db47012b`). It loads
the server environment, forces hybrid search, disables graph expansion/reranking,
and performs one untagged warm-up. A nearby corpus count was 2,067 rows,
including 130 tagged `identity` and two tagged `cross-repo`.

| Case | p50 (ms) | Script p95 (ms) | Maximum (ms) | Last-run count / wrong tags |
|---|---:|---:|---:|---|
| Untagged | 63.4 | 70.4 | 72.7 | 16 / n/a |
| Common tag | 62.8 | 73.6 | 74.7 | 17 / 0 |
| Sparse, matching | 54.6 | 83.8 | 682.6 | 2 / 0 |
| Sparse, weak | 56.3 | 75.5 | 276.0 | 2 / 0 |
| Nonexistent tag | 18.4 | 19.5 | 294.8 | 0 / 0 |

[Benchmark output](tagged-search-benchmark-2026-10-13.json) retains all fields
emitted by the existing script, with the machine-local root normalized to a
checkout/commit reference. Its p95 is `sorted[int(.95 * (n - 1))]`, the
ninth observation out of ten; report the maximum alongside it. Correctness
fields describe the last iteration, whereas the live probes below record
every response. Small samples do not estimate a population tail reliably.
Different code, corpus, and cache state preclude a causal speed comparison with
August. Reproduce locally with:

```sh
python3 scripts/ops/bench_kg_tag_filter.py "$PWD" 10
```

## Post-deployment probes

The owning deploy command advanced production from `b0dfa98aa` to `0db47012b`
and applied migration 074. The following calls then ran ten times each through
MCP `use_tool`, with `include_details=false` and full response mode. All used
`hybrid_rrf`. Wall-clock timings include transport and middleware; they are
not comparable to the retrieval-stage timings above.

| Probe | Result in all ten calls | Median / maximum (ms) |
|---|---|---:|
| Original dogfood: `query="dogfood friction", status="open", tags=["__dogfood_probe_tag_missing_20260822__"], limit=5` | Zero rows | 225 / 3,945 |
| Sparse weak: `query="quarterly budget forecast", tags=["cross-repo"], limit=20` | Both tagged rows, including original target | 308.5 / 326 |

[Raw live samples](tagged-search-live-probes-2026-10-13.json) retain every
timing, success flag, result ID, and returned tag set. The original target is
`2026-06-13T20:55:22.147315+00:00`. A second legitimate cross-repo note,
`2026-10-13T18:43:20.747647+00:00`, was written by another agent today. This
explains the count of two rather than the one in August; no probe rows were
created. Both current sparse cases find the original target.

The first original-dogfood call after the restart took 3,945 ms; the following
nine took 202–231 ms. It remains in the data. Restart/cold state is a plausible
factor, not an established cause. The same weak-query MCP probe before the
unrelated notices deployment had a median of 271.5 ms, versus 308.5 ms after
(+37 ms, about 14%). These sequential samples do not isolate deployment,
cache, transport, or background-load effects, and no latency acceptance
threshold was agreed. They establish current observed latency, not proof of no
regression. Neither comparison reintroduces the old wrong-tag or lost sparse
target behavior. Separate live verification found malformed successful notice
responses; that repair belongs to `codex/notice-response-fix-1013`, not this
search evidence change.

The finding ID is an audit fingerprint, **not** a KG discovery ID. Its original
records are `audit.events` IDs 64 and 65, type `dogfood_friction_finding`, with
payload fingerprint `bdb18ee4e52ee7d6`. The append-only events are preserved;
this document links the new measurements to them without rewriting history or
posting a false recurrence. Progress also belongs on the original KG usability
audit, `2026-10-13T06:48:37.234783+00:00`.

## Disposition

The pre-rollout condition has a pre-merge evidence artifact, and the
post-deployment correctness/latency condition now has recorded live probes.
Fresh review must assess the remaining latency qualifications. This report
requests an evidence disposition, not a new numerical standard or an
authorization waiver. The existing surfacing mechanism recognizes an accepted
new review naming the old session, or a merged PR explicitly declaring
`Answers dialectic 06abb3f7a18cce6f`; the original transcript remains inspectable.
