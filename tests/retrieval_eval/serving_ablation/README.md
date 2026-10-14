# Frozen KG serving-path ablation — 2026-10-14 UTC

**The implicit AGE relevance weights account for most of the measured search
gap in this pilot.** With the same stored BGE vectors, corpus, query vectors,
mode routing, fusion and authority policy, removing connectivity/age/status
weights raises known-target top-five from 28/48 to 46/48. This is a local frozen
replay, not a measurement of a deployed fix or a representative quality score.

Classification: **fix**. The handler now asks its semantic backend for cosine
relevance in both semantic-only and hybrid searches. Previously the AGE backend
silently mixed 30% connectivity into similarity, multiplied resolved records by
0.6, and applied a 90-day age term before the handler received its candidates.
A resolved incident may be exactly the diagnosis being sought. These weights
could bury it beneath recent, open or well-connected but less relevant notes.

The existing lifecycle visibility predicates, caller-requested recency weight,
imported/channel authority penalties, RRF, similarity floor and routing remain.
Direct callers of the AGE backend still have its existing weighting options and
defaults; this repair changes the shared-memory search handler's request.
No model channel, schema, deployment setting or new public parameter is added.

## Controlled inputs and replay

The [embedding pilot](../embedding_compare/report_2026-10-14.md) supplied its
unchanged 48 known-item labels, paired over 24 topics, and its cached BGE query
vectors. The sample includes 24 genuine previously logged searches and 24
authored paraphrases; target labels are purposive and not exhaustive relevance
judgments. All labeled targets remain in scope. No labels were changed or cases
dropped after inspecting scores.

A new repeatable-read, read-only database snapshot at
**2026-10-14T04:44:39.402699Z** froze document text, provenance, statuses,
timestamps, stored BGE vectors, graph edge counts, PostgreSQL vector results and
full-text results. Its row count is 2,074, with 702 default-visible rows. This is
not the earlier encoder experiment's corpus; the additional row is retained.

`scripts/eval/kg_serving_ablation.py` runs the actual `_run_text_search`,
`_blend_with_connectivity`, `get_connectivity_scores_batch`, filtering, RRF and
authority code against frozen storage inputs. The real handler runs in every
variant. Each counterfactual changes only the named weights, visibility order,
fusion or authority stage. Its clock is frozen, including the original backend's
naive local-clock behavior (America/Denver). Graph expansion and cross-encoder
reranking are disabled, matching the observed service configuration.

The `production_exact` and `production_ann` variants retain the pre-fix weights
explicitly even when this script runs with the patched handler. The latter name
means the captured PostgreSQL vector query, **not proof of an ANN index scan**:
the saved EXPLAIN chose a sequential scan and exact distance sort at this corpus
size. PostgreSQL and the NumPy exact variant produce identical target metrics.
No approximate-index loss was observed here.

The reconstructed baseline matches the earlier live service's target rank on
44/48 queries and its aggregate hit@1/5/10 counts exactly. Its MRR@10 is 0.4859
versus 0.4864 live. Different capture times, query-encoding precision and ties
still prevent claiming a bit-for-bit historical replay. The causal comparisons
below are between variants of the **same new frozen snapshot**.

## Stage results

MRR@10 rewards finding the pinned target earlier, with zero beyond rank ten.
The individual-weight rows start from dense cosine; removal rows start from the
full pre-fix serving configuration. Their effects interact and are not additive.

| Frozen configuration | Target first | Target top 5 | Target top 10 | MRR@10 |
|---|---:|---:|---:|---:|
| Dense cosine, visible corpus | 37/48 | 46/48 | 47/48 | 0.845 |
| Dense cosine, visibility after pool limit | 37/48 | 46/48 | 47/48 | 0.845 |
| Dense + connectivity only | 17/48 | 38/48 | 45/48 | 0.530 |
| Dense + status only | 20/48 | 22/48 | 25/48 | 0.445 |
| Dense + age only | 14/48 | 30/48 | 37/48 | 0.422 |
| Dense + all three backend weights | 8/48 | 16/48 | 21/48 | 0.241 |
| Weighted backend + auto fusion, before authority | 19/48 | 26/48 | 30/48 | 0.463 |
| Full pre-fix serving configuration | 19/48 | 28/48 | 37/48 | 0.486 |
| Full configuration, remove status weight only | 23/48 | 36/48 | 41/48 | 0.591 |
| Full configuration, remove age weight only | 20/48 | 32/48 | 35/48 | 0.542 |
| Full configuration, remove connectivity only | 17/48 | 28/48 | 35/48 | 0.458 |
| **Patched handler: remove all three backend weights** | **37/48** | **46/48** | **48/48** | **0.852** |

Fusion and authority help the weighted baseline in this sample. Removing fusion
alone lowers MRR to 0.298. Moving visibility before candidate truncation does
not recover the baseline's losses; with all old weights retained it lowers MRR
to 0.446, while with cosine relevance it leaves target metrics unchanged.
This experiment supports repairing implicit scoring, not disabling fusion or
the authority contract, nor claiming post-limit filtering can never hurt.

The patched handler's real forwarded kwargs, using the frozen PostgreSQL
candidate lists, reproduce the no-backend-weights counterfactual's target metrics.
Entire top-20 lists agree on 46/48 queries; small distance/tie differences change
non-target ordering in two. The stage output is in
[results_2026-10-14.json](results_2026-10-14.json), with corpus/label/source hashes,
per-query target ranks and the sanitized SQL plan. Full candidates and KG text
remain local.

## Gains, regressions and independent judging

The new ranking improves known-target MRR on **29 queries**, ties 16 and loses
three. Logged-query top-five rises from 20/24 to 24/24; paraphrase top-five from
8/24 to 22/24. These are diagnostic results on the development pilot, not held-out
generalization evidence.

Two losses move a pinned incident note from first to second: the facilitation
reaper and silence-probe searches. The freeze-policy paraphrase is a real loss:
the current policy target moves from first to tenth. Explicit
`recency_half_life_days=30` restores it to first in the same frozen replay. That
is an illustration of caller-directed recency, not a new default or a reason to
hide this regression. Current-policy search still needs broader evaluation.

Antigravity independently judged the union of before/after first results for all
three target-rank losses plus five seeded-random improvements (seed 2026101402).
The sixteen query-document pairs were shuffled, with old/new labels and original
targets hidden until after grades were saved. It judged **3/8** old first results
and **7/8** new first results as directly useful: five improvements, two ties and
one regression. The freeze case was the regression. Both incident alternatives
were relevant; one carries corrections that still require reading in context.

The [judgment receipt](blind_judgment_2026-10-14.json) separates relevance from
whether a claim is current, historical or superseded. Resolved is not synonymous
with obsolete. This selected top-one check is not an unbiased quality estimate;
three long records had omitted middle text, and linked companion notes were not
provided. The CLI did not report its exact underlying model. The first host call
timed out and terminated without grades; one retry through the existing isolated
CLI client completed. No timed-out response is counted as evidence.

## Reproduction and validation

Requires the repository's Python dependencies plus numpy and asyncpg. Capture
expects the existing BGE query-vector artifact and its `.json` metadata from the
embedding pilot. It uses one read-only transaction and creates no tables.

```sh
python scripts/eval/kg_serving_ablation.py capture \
  --vectors /path/to/private/bge.npz \
  --labels tests/retrieval_eval/embedding_compare/labels.json \
  --output /path/to/private/serving-snapshot.json
python scripts/eval/kg_serving_ablation.py replay \
  --snapshot /path/to/private/serving-snapshot.json \
  --labels tests/retrieval_eval/embedding_compare/labels.json \
  --server-timezone America/Denver \
  --output /path/to/private/serving-results.json
```

Capture refuses an existing snapshot and replay refuses changed labels or missing/
out-of-scope targets. The supplied timezone reproduces the original backend clock;
set it to the captured serving host's timezone on another installation.

Regression tests exercise the real AGE semantic and blend implementations through
the search handler with fixed storage/encoder inputs. Eight of the new behavior
checks failed before the fix; all eleven pass after it. Coverage includes semantic,
hybrid and auto routing, resolved historical answers, explicit recency, authority,
archived/cold visibility, lexical fusion and the similarity floor. The focused
search/backend/authority suites passed 447 tests; three additional replay tests
cover historical weights versus the patched caller and frozen-label guards.
The final `test-cache.sh --quick` selection passed **560 tests**, including the
existing retrieval metrics and serving-path suites. Ruff, strict documentation
health and whitespace checks passed. All eighteen published variants' hit/MRR
aggregates were reconciled against their target-rank maps.

No production model or service was changed to obtain these measurements. BGE
remains the baseline; the earlier Qwen/Gemma comparison remains useful for a later
model decision after broader independently labeled search evaluation.
