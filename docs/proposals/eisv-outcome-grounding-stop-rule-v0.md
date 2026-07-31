# EISV outcome-grounding: the measured bound and a stop rule

Status: proposed, 2026-07-31
Scope: whether per-agent EISV / prior-state adds predictive signal for
externally-verified bad outcomes over a previous-outcome baseline.
Supersedes the open-ended framing in `eisv-grounding-next-move-v0.md` §"what
would change our mind".

## Summary

The question is now answered at the resolution this fleet can supply, and the
answer is negative with a **quantified bound**: any lift EISV adds over the
boring previous-outcome baseline is **smaller than ≈ 0.05 AUC**.

That is a different and much stronger statement than the previous
"underpowered, unknown". It became available because the probe stopped
comparing a selected maximum against an implicit null of zero (#1422), which
made the noise floor measurable for the first time.

This document pre-registers a single confirmatory read and a kill criterion, so
the question stops being re-opened every time the label count moves.

## Why this needs a stop rule at all

The ablation matrix reports the **best** of ~7 candidate models per slice. Until
#1422 that maximum was displayed against a null of zero, so noise read as
signal. The practical consequence was a recurring loop: labels accrue → someone
re-runs the probe → a positive maximum appears → an audit shows it is an
artifact → nothing is learned.

That loop ran again on 2026-07-31. Within a single hour the winning candidate
changed identity twice as two labels landed, and the pre-#1422 instrument
reported `auc_delta +0.162, CI [0.006, 0.315]` — a confidence interval excluding
zero — on the same data where the corrected instrument reports `-0.033`
(selective p = 0.857).

A stop rule is what converts "we keep finding nothing" into "we established a
bound and stopped looking".

## The measured detection threshold

The selective null permutes whole EISV readings between (agent, prior-state
snapshot) clusters, holding labels fixed. Its 95th percentile is the smallest
lift distinguishable from the best-of-candidates noise floor, i.e. the minimum
detectable lift (MDL).

Measured on `task` scope, 365 d, lead 30 min, holding good rows fixed and
subsampling bad clusters (script: `scripts/analysis/eisv_ablation_matrix.py`,
`estimate_selective_null`):

| independent bad clusters | bad rows | null median | **MDL (null p95)** |
|---|---|---|---|
| 10 | 10 | — | null not formable |
| 20 | 39 | +0.003 | +0.324 |
| 40 | 90 | +0.014 | +0.149 |
| 50 | — | ~0.000 | +0.043 … +0.086 (seed-dependent) |
| 80 | 134 | +0.007 | +0.057 |
| **101 (all available)** | **195** | **+0.006** | **+0.048 / +0.055 / +0.050** |

The bottom row is three independent seeds at 400 resamples: **MDL ≈ 0.05, and
seed-stable**. At 50 clusters the estimate still swings by a factor of two
depending on which clusters are drawn, so ~100 clusters is roughly where this
measurement becomes trustworthy — which is where the fleet now is.

Read MDL as a detection threshold, not a power calculation. Power at exactly the
threshold is ~50%; an effect would need to sit comfortably above it to be caught
reliably. That makes the bound below conservative in the right direction.

## The bound

At 101 independent bad clusters / 195 bad rows:

| slice | observed best `auc_delta` | selective p |
|---|---|---|
| task / 365 d / lead 30 | **−0.033** | 0.857 |
| task / 365 d / lead 0 | +0.015 | 0.333 |

Both sit well inside the noise floor. Combined with MDL ≈ 0.05:

> If EISV/prior-state added ≥ 0.05 AUC over the previous-outcome baseline, this
> measurement would show it. It does not. The effect, if any, is below 0.05.

An effect that small is below operational relevance: a governance signal worth
acting on would have to clear the boring baseline by considerably more than
0.05 to justify the coupling.

## What more labels would buy

Independent bad clusters accrue at ~25–29 per month (15 / 21 / 42 / 23 over
2026-04 … 2026-07). MDL falls roughly as 1/√K, so **halving MDL to ≈ 0.025 needs
about 4× the clusters — on the order of a further year** — to tighten a bound
that is already below the threshold of operational interest.

"Capture more labels" was already found to be the wrong lever for a different
reason (per-agent balance, not volume, in `eisv-grounding-next-move-v0.md`).
This is a second, independent reason: the marginal bound improvement does not
repay the wait.

## Pre-registered gate

**One confirmatory read, 2026-12-01.** By then accrual should put the slice near
200 bad clusters (MDL ≈ 0.035), i.e. one meaningful step of extra resolution.

Command (run from a checkout of `master`):

```
python3 scripts/analysis/eisv_ablation_matrix.py \
    --scopes task --windows 365 --leads 0,30 \
    --uncertainty-resamples 2000 --selective-null-resamples 400
```

**PASS** — outcome-grounding remains open, and Stage B may be reconsidered —
requires all of:

1. `Selective p` ≤ 0.05 on at least one of the two lead slices;
2. observed `AUC delta` > `Null max p95` on that slice;
3. `Bad clusters` ≥ 150 (below this the MDL estimate is not yet stable);
4. the winning candidate is the same family as at this read's other lead slice
   — an argmax that changes with a nuisance parameter is noise-mining.

**FAIL — the kill criterion.** If any condition is unmet, EISV outcome-grounding
is **closed**: no further scheduled outcome-prediction reads, no Stage B, and the
bound above becomes the standing published claim. Reopening requires a *new
premise* — a materially different label channel or measurement process — not
simply more of the same labels. This mirrors the individuality-v2 kill criterion,
which was honoured on 2026-07-30.

Do not adjust these thresholds after seeing the read. The point of writing them
down now is that they were chosen before the data existed.

## What continues regardless

Closing outcome-grounding does not retire EISV. The label-free path is
unaffected and is where the earned claims live:

- policy-coherence invariants (monotonicity, graded regime-injection recall,
  order-sensitivity) — all passing;
- proprioceptive state estimation as *telemetry*, which is the deployed framing
  already (Φ is telemetry-only; the behavioural path holds verdict authority).

The honest public statement after a FAIL is: *EISV is an instrument for
self-state estimation whose outcome-predictive lift over a trivial baseline is
bounded below 0.05 AUC on a fleet of ~100 independent adjudicated failures.*
That is a publishable negative result, and it retires the risk rather than
leaving it open.

## Do not

- **Do not re-run the probe ad hoc between now and the read date and treat a
  positive maximum as news.** That is the loop this document exists to end. The
  matrix now prints `Null max median` next to `AUC delta` precisely so a
  selected maximum cannot be mistaken for an effect.
- **Do not quote `AUC delta` without `Bad clusters` and `Selective p`.**
- **Do not read the bootstrap `AUC delta 95% CI` as significance.** It is
  computed for the already-selected winner, so it is conditional on selection
  and will keep excluding zero on noise; the 2026-07-31 pre-fix run showed
  exactly that.
- **Do not widen the label definition to raise the count.** The classifier that
  now gates `test_failed` withholds TDD red steps and deliberately induced
  failures; relaxing it would inflate `Bad` with non-outcomes and lower the
  quality of the very bound this rule rests on.

## Provenance

Instrument fixes: #1422. All numbers here were produced from `master` at
`5a040940` against the live governance database on 2026-07-31.
