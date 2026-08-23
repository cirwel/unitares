# Frozen EISV ablation read — 2026-08-09

**Status:** Dated descriptive non-detection; **not a negative result or an AUC
bound**. Generated 2026-08-11 from
UNITARES v2.17.0 (`5f050f040fcd77699dd0f473c5ddf15710fdea84`) against the
single-operator deployment database, frozen at `2026-08-09T20:00:00Z`.

This record preserves the overall rows behind the current evaluator-facing
claim. It is not the preregistered 2026-12-01 confirmatory read, evidence of
prevention, or an independent deployment result.

## Command

```bash
python3 scripts/analysis/eisv_ablation_matrix.py \
  --read-protocol reproduction \
  --read-id frozen-2026-08-09-reproduction-YYYYMMDD-HHMMSS \
  --acknowledge-contamination \
  --scopes strict,task --windows 30,90 --leads 0,5,30 \
  --anchor-scope trusted --as-of 2026-08-09T20:00:00Z \
  --telemetry-strata source,warmup,enforcement,missingness
```

The default selection-aware null uses 200 whole-cluster permutations with seed
0. Replace the read-ID timestamp before running; every reproduction is a
disclosed outcome access and receives its own receipt. Those permutations
reassign whole EISV readings between `(agent, prior-state
snapshot)` clusters and hold the **labels fixed**, so the previous-outcome
baseline is identical in every resample. (This sentence previously said "label
permutations", copying a since-corrected `--selective-null-resamples` help
string; the null never shuffled labels, and no row below changes.) `trusted` requires an `external_signal` anchor and a joinable prior EISV
snapshot. The BEAM harness lane is excluded by default. No uncertainty bootstrap
was requested for this documentation audit.

## Overall rows

| Scope | Window | Lead | Rows | Bad | Bad clusters | Agents | Prior state | Baseline AUC | Baseline Brier | Selected candidate | AUC Δ | Null max median | Selective p | Brier improvement | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|
| strict | 30d | 0m | 223 | 53 | 29 | 16 | 223 | 0.427 | 0.1697 | `prior_risk_binned` | 0.151 | 0.163 | 0.562 | 0.0039 | `NON_DETECTION` |
| strict | 30d | 5m | 223 | 53 | 28 | 16 | 218 | 0.427 | 0.1697 | `prior_risk_binned` | 0.238 | 0.161 | 0.189 | 0.0116 | `NON_DETECTION` |
| strict | 30d | 30m | 223 | 53 | 28 | 16 | 189 | 0.427 | 0.1697 | `previous_bad_plus_dispersion` | 0.341 | 0.164 | 0.075 | 0.0007 | `NON_DETECTION` |
| strict | 90d | 0m | 224 | 53 | 29 | 16 | 224 | 0.427 | 0.1686 | `prior_risk_binned` | 0.150 | 0.163 | 0.567 | 0.0038 | `NON_DETECTION` |
| strict | 90d | 5m | 224 | 53 | 28 | 16 | 218 | 0.427 | 0.1686 | `prior_risk_binned` | 0.237 | 0.177 | 0.219 | 0.0118 | `NON_DETECTION` |
| strict | 90d | 30m | 224 | 53 | 28 | 16 | 189 | 0.427 | 0.1686 | `prior_s_binned` | 0.343 | 0.162 | 0.080 | 0.0139 | `NON_DETECTION` |
| task | 30d | 0m | 226 | 53 | 29 | 16 | 226 | 0.435 | 0.1679 | `prior_risk_binned` | 0.147 | 0.153 | 0.527 | 0.0038 | `NON_DETECTION` |
| task | 30d | 5m | 226 | 53 | 28 | 16 | 221 | 0.435 | 0.1679 | `prior_risk_binned` | 0.233 | 0.156 | 0.164 | 0.0124 | `NON_DETECTION` |
| task | 30d | 30m | 226 | 53 | 28 | 16 | 191 | 0.435 | 0.1679 | `previous_bad_plus_dispersion` | 0.341 | 0.144 | 0.070 | 0.0007 | `NON_DETECTION` |
| task | 90d | 0m | 227 | 53 | 29 | 16 | 227 | 0.435 | 0.1669 | `prior_risk_binned` | 0.147 | 0.144 | 0.488 | 0.0037 | `NON_DETECTION` |
| task | 90d | 5m | 227 | 53 | 28 | 16 | 221 | 0.435 | 0.1669 | `prior_risk_binned` | 0.232 | 0.149 | 0.159 | 0.0126 | `NON_DETECTION` |
| task | 90d | 30m | 227 | 53 | 28 | 16 | 191 | 0.435 | 0.1669 | `prior_s_binned` | 0.343 | 0.167 | 0.085 | 0.0101 | `NON_DETECTION` |

## Errata — what this transcription dropped (added 2026-08-23)

The rows above are a **partial** transcription of the harness output. Two
columns the harness emits, and its own reading instructions require, are
missing and were never recorded elsewhere:

- **`Null max p95`** — the decision threshold. `Selective p` alone does not show
  how large a lift this cohort would have needed. The pre-registered stop rule
  makes `AUC delta > Null max p95` a separate PASS condition, so the column is
  load-bearing, not decorative.
- **`Null clusters`** — the total number of permutable units. The harness prints
  "few clusters bound how small `Selective p` can get, so read them together",
  and every downstream citation quotes the p-values without it. `Bad clusters`
  (28–29) is a different count and is not a substitute.

Also dropped: `Prior risk`, `Envelope`, `AUC delta 95% CI`, `Brier improvement
95% CI`, `Brier perm p`, and `Beats both?`.

Recovering these would require re-running the frozen command against the
deployment database at the same `--as-of` cutoff. Do not perform another live
read solely to repair this historical transcription; leave the columns marked
missing. Cite the p-values in this table **only** alongside the power
characterisation in
[`falsifiability-power-audit-2026-08-23.md`](falsifiability-power-audit-2026-08-23.md),
which measures what this instrument could have detected on a cohort of this
shape. The rows themselves are unchanged.

## Interpretation

The unadjusted selected candidates sometimes improve both ranking and
calibration. That is not the correct final comparison: the report selected the
largest AUC delta from roughly seven candidates, so it compares that maximum
with the distribution of maxima under the null. None of the 12 overall slices
clears p < 0.05; the smallest selective p is 0.070.

The frozen rows contain no `eisv.telemetry.v1` envelopes, so the marginal
source/warmup/enforcement/missingness output is entirely
`legacy/no-envelope`. The result does not estimate a causal policy effect and
does not show that any incident was prevented. `Bad clusters` counts
`(agent, prior-state snapshot)` permutation blocks because multiple outcomes can
share one constant feature reading. It is not proof that outcomes are
independent between blocks, so the table reports bad rows and agents alongside
the block count.

The preregistered stop rule and next confirmatory date live in
[`../proposals/eisv-outcome-grounding-stop-rule-v0.md`](../proposals/eisv-outcome-grounding-stop-rule-v0.md).

## Protocol disclosure — added 2026-08-23

After this cutoff, two six-hour automation jobs repeatedly queried live outcome
discrimination: the ablation watchdog ran 51 times (42 completed), and the
dogfood/ablation guard ran 52 times (43 completed). The former computed two
selection-aware matrices per successful run; the latter computed two point
estimate matrices with null resampling disabled. Both were paused on 2026-08-22.

These later reads do not alter the numbers frozen above. They do invalidate any
future description of the December analysis as the only post-registration read
or as unqualified analysis-blind. Counts, implications, and containment are
recorded in
[`../ontology/falsification-design-system-audit-2026-08-23.md`](../ontology/falsification-design-system-audit-2026-08-23.md).
