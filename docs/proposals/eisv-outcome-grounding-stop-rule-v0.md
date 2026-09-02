# EISV outcome-grounding: scope correction and a stop rule

Status: proposed, 2026-07-31; evidence-scope correction, 2026-08-17;
power-characterisation correction, 2026-08-23
Scope: whether per-agent EISV / prior-state adds predictive signal for
externally-verified bad outcomes over a previous-outcome baseline.
Supersedes the open-ended framing in `eisv-grounding-next-move-v0.md` §"what
would change our mind".

## Summary

The fixed 2026-12-01 final decision read and its operational kill criterion
remain in force. It may no longer be described as the only post-registration
outcome read or as unqualified analysis-blind. The 2026-07-31 numeric bound does
not remain in force.

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

## Protocol deviation — disclosed 2026-08-23

The instruction below said not to rerun the probe ad hoc. Automation nevertheless
exposed live discrimination results every six hours after the frozen cutoff:

- the UNITARES ablation watchdog ran 51 times, completing 42 runs that invoked
  the outcome inventory and two selection-aware matrices; and
- the UNITARES dogfood/ablation guard ran 52 times, completing 43 runs that
  invoked the inventory and two matrices with null resampling disabled to check
  harness-lane hygiene.

Both jobs were paused on 2026-08-22. The guard is being converted to synthetic
contract tests, and the watchdog fails closed before data access without an
explicit protocol-contamination override. The full audit is
[`../ontology/falsification-design-system-audit-2026-08-23.md`](../ontology/falsification-design-system-audit-2026-08-23.md).

This deviation does not change the fixed date, cohort, thresholds, four PASS
conditions, or operational closure commitment. It changes the epistemic claim:
the December report must disclose the interim accesses, identify any analysis,
label, scope, collection, or narrative choices they affected, and include a
read-specific power analysis. It may not claim clean single-read blinding.

## Pre-declared sensitivity cohort — declared 2026-09-02

The registered read runs with the fixture rule it was registered with: the
`registered` rule, under which a server-stamped `calibration_excluded` classifies
the row as fixture traffic whatever its cause. `eisv_ablation_matrix.py` pins
that rule for this read through `REGISTERED_READ_MANIFEST` and rejects any
other value, and the registered command names it explicitly so the contract
depends on no code default. (The shared default of the non-protocol
instruments moved to `corrected` on 2026-09-02; it cannot reach this read.)

Declared now, before the read: the December report will present, beside the
registered result and **without authority**, the same command run a second time
under the corrected rule (a scraped confidence excludes a row from calibration,
not from evidence):

```
python3 scripts/analysis/eisv_ablation_matrix.py \
    --read-protocol reproduction \
    --read-id eisv-outcome-grounding-2026-12-01-sensitivity \
    --acknowledge-contamination \
    --fixture-rule corrected \
    --scopes task --windows 365 --leads 0,30 \
    --anchor-scope trusted --exclude-harness-lanes beam \
    --as-of "$READ_CUTOFF" \
    --uncertainty-resamples 2000 --selective-null-resamples 400
```

Same `READ_CUTOFF`, its own receipt, reported as a sensitivity analysis. The
registered predicate decides the four PASS conditions. If condition 3 fails on
the registered cohort, whether a corrected instrument and producer contract are
the "materially different measurement process" the reopening clause requires
is the operator's judgment, not a consequence of this declaration.

Selection record: the decision packet
[`outcome-fixture-conflation-decision-packet-v0.md`](outcome-fixture-conflation-decision-packet-v0.md)
offered four branches; on 2026-09-02 the operator delegated the selection to the
working agent ("proceed on your own accord, best for federation") and branch R1,
this declaration, was selected under that criterion.

## Interim access — disclosed 2026-09-02

While preparing the decision packet
[`outcome-fixture-conflation-decision-packet-v0.md`](outcome-fixture-conflation-decision-packet-v0.md),
an agent session ran `scripts/analysis/legacy_coherence_dependency_shadow.py`
three times against the live database: the default 365-day `task` scope,
`--window-days 21` in `task` scope, and `--window-days 21 --scope strict`. The
script reads the matching live outcome rows from the database and then applies
the fixture predicate; it computes discrimination statistics and carries no
read-protocol guard. All three runs returned 0 eligible outcomes after that
filter, so no discrimination result was computed or exposed. `scripts/analysis/outcome_inventory.py`
was also run, which the falsification audit permits. This changes no cutoff,
cohort, threshold or condition; it is recorded so the December report can
list every known interim access.

## Power-characterisation correction — disclosed 2026-08-23

The first database-free power audit reused `(agent, prior-state snapshot)`
cluster IDs as synthetic outcome `row_key` values. Production rows use unique
`outcome_id`. Because candidate/baseline pairing indexes by `row_key`, the
duplicates overwrote rows and invalidated the published simulation figures.
Those figures, including 3% power at AUC about 0.57 and 80% near AUC 0.82, are
withdrawn. The correction does not alter the frozen production matrix's own
selective p-values.

The corrected probe requires the observed row, bad-row, total null-cluster, and
agent counts and reports unscorable trials in its denominator with a binomial
interval. It remains an optimistic synthetic sensitivity scenario, not a proven
upper bound on the real slice. The frozen record omitted total `Null clusters`,
so its read-specific power cannot be reconstructed from the preserved table.

This document also referred to a “predeclared smallest relevant effect” without
naming one. As of this correction, no beta, AUC delta, or equivalent effect size
fills that slot. It must be declared by the operator before any further live
outcome-discrimination access; otherwise the December read can implement the
operational stop rule but cannot earn a power-qualified scientific `REFUTED`.

## Pre-registered gate

**One fixed scheduled operational decision read, 2026-12-01.** Before querying, record one UTC
cutoff and use it for `READ_CUTOFF` below. Do not move the cutoff after seeing
output.
This correction changes neither the date nor the four PASS conditions; it makes
the intended cohort executable and prevents an unsupported bound from being
published automatically after a FAIL.

Command (run from a checkout of `master`):

```
READ_CUTOFF="PREDECLARED_UTC_CUTOFF"
python3 scripts/analysis/eisv_ablation_matrix.py \
    --read-protocol registered \
    --read-id eisv-outcome-grounding-2026-12-01 \
    --not-before 2026-12-01T16:00:00Z \
    --acknowledge-contamination \
    --scopes task --windows 365 --leads 0,30 \
    --anchor-scope trusted --exclude-harness-lanes beam \
    --fixture-rule registered \
    --as-of "$READ_CUTOFF" \
    --uncertainty-resamples 2000 --selective-null-resamples 400
```

(`--fixture-rule registered` added 2026-09-02: it names the fixture predicate
the read was registered with, which the CLI already pins for registered reads,
so the execution contract does not depend on a code default. It changes no
cohort. Since 2026-09-02 the shared default of the non-protocol instruments is
`corrected`; this read's rule comes from `REGISTERED_READ_MANIFEST` in
`eisv_ablation_matrix.py`, which fixes `registered` for this read's id and its
`-retry-<n>` forms, so the default cannot reach it.)

The CLI validates this declaration and writes an atomic access receipt before
the database query. It refuses an early read, a future `--as-of`, an undeclared
read, or reuse of the same read ID. If an attempt fails after its receipt is
written, any retry uses a new ID of the form
`eisv-outcome-grounding-2026-12-01-retry-<n>`, the only suffix the protocol
manifest admits, and the report discloses both attempts; deleting
the receipt to preserve a single-read story would itself be a protocol violation.
The contamination acknowledgement records the already-known interim accesses;
it does not turn the scheduled operational read into clean confirmatory evidence.

After the matrix reports the primary slice's `Rows`, `Bad`, `Null clusters`, and
`Agents`, run the database-free power probe with that exact shape:

```
python3 scripts/analysis/ablation_power_probe.py \
    --rows <Rows> --bad <Bad> --clusters <Null clusters> --agents <Agents> \
    --trials 100 --resamples 400 --seed 0
```

`--bad` controls the simulated class balance; it must come from this read rather
than silently reusing the 2026-08-09 cohort. The probe is a deliberately clean,
optimistic sensitivity scenario, not a proven upper bound. Report its power at
the independently predeclared smallest relevant effect alongside the real
slice's null width before assigning a scientific inference status. If that
effect remains unspecified, report the scientific inference as `INCONCLUSIVE`.

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
cutoff, bad rows, permutation blocks, agents, selected delta, null p95,
selective p, and read-specific power against a predeclared smallest relevant
effect. A support-qualified non-detection is `INCONCLUSIVE`, not `REFUTED`, if
that power is inadequate. The withdrawn `0.05` value never fills this slot
automatically.

Do not adjust these thresholds after seeing the read. The point of writing them
down now is that they were chosen before the data existed.

### Feasibility diagnostic for condition 3 — disclosure corrected 2026-08-23

This is a post-registration, exploratory support-feasibility note. It uses only
the support counts already published in the frozen 2026-08-09 artifact; it adds
no database access or discrimination read. It changes no cutoff, cohort, label
definition, threshold, PASS condition, kill criterion, or reopening rule, and it
cannot authorize an early stop or any change before the registered read.

The frozen **task** slices report 29 bad clusters at lead 0 and 28 at lead 30 in
both their 30- and 90-day windows. Relative to those observed counts, condition
3's 150-block threshold leaves arithmetic gaps of 121 and 122 blocks. The
registered slice, however, uses a 365-day window, which the frozen artifact did
not measure. Equality at 30 and 90 days establishes only that extending the
lookback from 30 to 90 days at the 2026-08-09 cutoff added no cluster keys. It
does not establish that the unmeasured part of the registered window adds none,
or that future accrual is supply-limited.

The previously disclosed “28 bad clusters over 254 days ≈ 3.4/month” and
“≈ 9.7×” acceleration are withdrawn. The 28 is a trailing-window slice count;
254 days is the general deployment interval from the first identity record, not
the exposure interval of this trusted, joinable analytic cohort. Dividing one by
the other does not estimate historical accrual. Under the additional, unverified
assumption that the wider window supplies no other blocks, the observed gaps
would require 121 or 122 additional distinct blocks during the 114-day interval
from the frozen cutoff to the registered date (about 32.3 or 32.6 per mean
month). Those are conditional required paces, not observed rates or forecasts.

The database-free diagnostic renders only this per-lead arithmetic and the
fixed-cutoff lookback comparison by default:

```
python3 scripts/analysis/support_reachability.py
```

The repository records no longitudinal series of eligible cluster-key additions
under the registered rules, so the historical rate, acceleration factor, and
condition 3 reachability remain **UNKNOWN**. Do not refresh this diagnostic with
live data before the registered read. The 2026-12-01 read remains in force
exactly as registered. If condition 3 is unmet, the interpretation already
specified above applies: closure for insufficient eligible evidence, not a
measured null or disproof.

## What continues regardless

Closing outcome-grounding does not retire EISV. The label-free path is
unaffected and is where the earned claims live:

- policy-coherence invariants (monotonicity, graded regime-injection recall,
  order-sensitivity) — all passing;
- proprioceptive state estimation as *telemetry*, which is the deployed framing
  already (Φ is telemetry-only; the behavioural path holds verdict authority).

The public statement after a FAIL is conditional on which gate failed. A
support-only failure says the registered question closed without enough
eligible evidence and remains untested, not disproved. A support-qualified
non-detection may report a bound only if the read has adequate predeclared power
for that bound, with the full provenance tuple above. The operational `FAIL`
closes scheduled work; it does not automatically earn a scientific `REFUTED`
status.

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
contaminated `all` default. The analysis tools now default to `trusted`; the
registered command still names the scope explicitly so its execution contract
does not depend on a mutable default. The trusted descriptive snapshot is
frozen at 2026-08-09T20:00:00Z and recorded in the operations artifact linked
above.
