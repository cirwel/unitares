# EISV outcome-grounding: scope correction and a stop rule

Status: proposed, 2026-07-31; evidence-scope correction, 2026-08-17
Scope: whether per-agent EISV / prior-state adds predictive signal for
externally-verified bad outcomes over a previous-outcome baseline.
Supersedes the open-ended framing in `eisv-grounding-next-move-v0.md` §"what
would change our mind".

## Summary

The single 2026-12-01 confirmatory read and its kill criterion remain in force.
The 2026-07-31 numeric bound does not.

The historical read used the ablation script's default `--anchor-scope all`.
That scope deliberately preserves a contaminated tracked series: it admits rows
outside the externally verified trusted-anchor population. Its 101 "clusters"
were `(agent, prior-state snapshot)` permutation blocks, not 101 independent
adjudicated failures. The resulting `≈ 0.05 AUC` minimum-detectable-lift (MDL)
estimate is therefore withdrawn as evidence for the question stated above.

This correction does not turn the old null into a positive result. It restores
the honest state to **unresolved pending the registered trusted-anchor read**.
The selection-aware null remains the right instrument; the execution contract
below now fixes its evidence scope and reporting units explicitly.

## Why this needs a stop rule at all

The ablation matrix reports the **best** of ~7 candidate models per slice. Until
#1422 that maximum was displayed against a null of zero, so noise read as
signal. The practical consequence was a recurring loop: labels accrue → someone
re-runs the probe → a positive maximum appears → an audit shows it is an
artifact → nothing is learned.

That loop ran again on 2026-07-31. Within a single hour the winning candidate
changed identity twice as two labels landed, and the pre-#1422 instrument
reported `auc_delta +0.162, CI [0.006, 0.315]` on the same all-scope data where
the selection-aware instrument reported `-0.033` (selective p = 0.857). This is
an instrument-correction exhibit, not externally verified performance evidence.

A stop rule converts repeated exploratory reads into one answerable decision.
It does not license carrying a result across evidence scopes.

## Historical 2026-07-31 read — withdrawn for target inference

The selective null permutes whole EISV readings between `(agent, prior-state
snapshot)` blocks, holding labels fixed. Its 95th percentile estimates the lift
needed to clear the best-of-candidates noise floor for that exact cohort. The
blocks preserve constant-feature dependence inside edit-test-retry bursts; they
are not proof that outcomes are independent between blocks.

Measured on `task` scope, 365 d, lead 30 min, holding good rows fixed and
subsampling bad clusters (script: `scripts/analysis/eisv_ablation_matrix.py`,
`estimate_selective_null`):

| bad permutation blocks | bad rows | null median | **MDL (null p95)** |
|---|---|---|---|
| 10 | 10 | — | null not formable |
| 20 | 39 | +0.003 | +0.324 |
| 40 | 90 | +0.014 | +0.149 |
| 50 | — | ~0.000 | +0.043 … +0.086 (seed-dependent) |
| 80 | 134 | +0.007 | +0.057 |
| **101 (all available)** | **195** | **+0.006** | **+0.048 / +0.055 / +0.050** |

The bottom row was stable across three random seeds at 400 resamples for the
historical all-scope cohort. That is a reproducibility fact about this
contaminated cohort, not a transferable bound for trusted anchors. Read MDL as a
detection threshold, not a power calculation.

At 101 bad permutation blocks / 195 bad rows, the historical read reported:

| slice | observed best `auc_delta` | selective p |
|---|---|---|
| task / 365 d / lead 30 | **−0.033** | 0.857 |
| task / 365 d / lead 0 | +0.015 | 0.333 |

These values must not be restated as a bound on externally verified outcomes.
The earlier `1/√K` accrual projection and operational-relevance conclusion also
depended on that transfer and are withdrawn.

## The two counts that were being conflated

The 101 and 26 counts do not estimate the same quantity:

- **101** was the number of `(agent, prior-state snapshot)` permutation blocks in
  the 2026-07-31 all-scope analytic slice. It included 195 bad rows and used no
  trusted-anchor predicate.
- **26** was a 2026-08-06 availability census over 83 bad `external_signal` rows,
  grouped by `(agent, session)`. It did not use the stop-rule outcome-type,
  snapshot, fixture, or harness-lane filters.

Neither count can be substituted for the other, plotted as one accrual series,
or called an independent-failure count. The stop-rule counter is the script's
`Bad clusters` output on the fully specified trusted analytic slice: a count of
prior-state permutation blocks. The read must also report bad rows and agents so
the dependence structure remains visible.

## Existing trusted snapshot — descriptive only

The frozen 2026-08-09 trusted-anchor matrix is the current citable dated
snapshot. Across its 12 strict/task × 30/90-day × 0/5/30-minute slices it has
223–227 outcomes, 53 bad rows, 28–29 prior-state permutation blocks, and 16
agents. None clears the selection-aware null (selective p = 0.070–0.567).

That snapshot is not the 2026-12-01 confirmatory read, does not establish a
standing AUC bound, and does not measure prevention or a causal policy effect.
Its frozen command and rows are recorded in
`../operations/eisv-ablation-frozen-2026-08-09.md`. No new discrimination read
was run for the 2026-08-17 correction.

## Pre-registered gate

**One confirmatory read, 2026-12-01.** Before querying, record one UTC cutoff and
use it for `READ_CUTOFF` below. Do not move the cutoff after seeing output.
This correction changes neither the date nor the four PASS conditions; it makes
the intended cohort executable and prevents an unsupported bound from being
published automatically after a FAIL.

Command (run from a checkout of `master`):

```
READ_CUTOFF="PREDECLARED_UTC_CUTOFF"
python3 scripts/analysis/eisv_ablation_matrix.py \
    --scopes task --windows 365 --leads 0,30 \
    --anchor-scope trusted --exclude-harness-lanes beam \
    --as-of "$READ_CUTOFF" \
    --uncertainty-resamples 2000 --selective-null-resamples 400
```

**PASS** — outcome-grounding remains open, and Stage B may be reconsidered —
requires all of:

1. `Selective p` ≤ 0.05 on at least one of the two lead slices;
2. observed `AUC delta` > `Null max p95` on that slice;
3. `Bad clusters` ≥ 150 on this trusted slice;
4. the winning candidate is the same family as at this read's other lead slice
   — an argmax that changes with a nuisance parameter is noise-mining.

Condition 3 retains the frozen 150-block eligibility threshold. The 2026-07-31
all-scope experiment no longer justifies saying that 150 is a trusted-scope
stability estimate; retaining the threshold is a conservative pre-commitment,
not a repaired empirical claim. `Bad clusters` here means prior-state
permutation blocks, not independent adjudicated failures.

**FAIL — the kill criterion.** If any condition is unmet, EISV outcome-grounding
is **closed**: no further scheduled outcome-prediction reads and no Stage B.
Reopening requires a *new premise* — a materially different label channel or
measurement process — not simply more of the same labels. This mirrors the
individuality-v2 kill criterion, which was honoured on 2026-07-30.

The report must name the failed conditions. If condition 3 fails, describe the
result as closure for insufficient eligible evidence, not as a measured null or
as disproof. If the support condition passes but a signal condition fails, any
published bound is recomputed from that read and reported with anchor scope,
cutoff, bad rows, permutation blocks, agents, selected delta, null p95, and
selective p. The withdrawn `0.05` value never fills this slot automatically.

Do not adjust these thresholds after seeing the read. The point of writing them
down now is that they were chosen before the data existed.

## What continues regardless

Closing outcome-grounding does not retire EISV. The label-free path is
unaffected and is where the earned claims live:

- policy-coherence invariants (monotonicity, graded regime-injection recall,
  order-sensitivity) — all passing;
- proprioceptive state estimation as *telemetry*, which is the deployed framing
  already (Φ is telemetry-only; the behavioural path holds verdict authority).

The public statement after a FAIL is conditional on which gate failed. A
support-only failure says the registered question closed without enough
eligible evidence and remains untested, not disproved. A support-qualified null
may report only the bound computed at the confirmatory read, with the full
provenance tuple above.

## Do not

- **Do not re-run the probe ad hoc between now and the read date and treat a
  positive maximum as news.** That is the loop this document exists to end. The
  matrix now prints `Null max median` next to `AUC delta` precisely so a
  selected maximum cannot be mistaken for an effect.
- **Do not quote `AUC delta` without anchor scope, cutoff, bad rows, bad
  permutation blocks, agents, null p95, and selective p.**
- **Do not call `Bad clusters` independent outcomes or adjudicated failures.**
  They are the permutation blocks induced by shared prior-state snapshots.
- **Do not compare the 26 `(agent, session)` availability groups with the
  stop-rule's prior-state blocks or the 150-block eligibility threshold.**
- **Do not read the bootstrap `AUC delta 95% CI` as significance.** It is
  computed for the already-selected winner, so it is conditional on selection
  and will keep excluding zero on noise; the 2026-07-31 pre-fix run showed
  exactly that.
- **Do not widen the label definition to raise the count.** The classifier that
  now gates `test_failed` withholds TDD red steps and deliberately induced
  failures; relaxing it would inflate `Bad` with non-outcomes and lower the
  quality of the very bound this rule rests on.

## Provenance

Instrument fixes: #1422. The withdrawn historical numbers were produced from
`master` at `5a040940` against the live governance database on 2026-07-31; at
that commit the command omitted `--anchor-scope` and therefore used the
contaminated `all` default. The trusted descriptive snapshot is frozen at
2026-08-09T20:00:00Z and recorded in the operations artifact linked above.
