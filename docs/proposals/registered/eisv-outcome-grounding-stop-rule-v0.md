# EISV outcome-grounding: scope correction and a stop rule

Status: proposed, 2026-07-31; evidence-scope correction, 2026-08-17;
power-characterisation correction, 2026-08-23; condition 4 clarification,
2026-09-23
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
[the dated evidence record](../../operations/eisv-ablation-frozen-2026-08-09.md). No new discrimination read
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
[`../ontology/falsification-design-system-audit-2026-08-23.md`](../../ontology/falsification-design-system-audit-2026-08-23.md).

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
[`outcome-fixture-conflation-decision-packet-v0.md`](../archive/outcome-fixture-conflation-decision-packet-v0.md)
offered four branches; on 2026-09-02 the operator delegated the selection to the
working agent ("proceed on your own accord, best for federation") and branch R1,
this declaration, was selected under that criterion.

## Interim access — disclosed 2026-09-02

While preparing the decision packet
[`outcome-fixture-conflation-decision-packet-v0.md`](../archive/outcome-fixture-conflation-decision-packet-v0.md),
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

### Pre-data clarification — 2026-09-23: what condition 4's "family" means

PASS condition 4 above says the winning candidate must be "the same family as
at this read's other lead slice". The word "family" appears nowhere else in
this document and is mapped onto the candidate set in no code, test, or
document (#2154). This block defines it before the registered cohort exists.
It is a clarification of an undefined term, not a change to any threshold,
cohort, cutoff, date, estimator, or command.

**Definition.** For this read, condition 4 is met if and only if the
`Best EISV/prior model` cell of the task / 365 d / lead 0 row is
byte-identical to the `Best EISV/prior model` cell of the task / 365 d /
lead 30 row in the matrix output of the single registered read
(`--read-id eisv-outcome-grounding-2026-12-01`, or the one `-retry-<n>` id
whose receipt the report discloses as the attempt that completed), and
neither cell is `-`. "Family" therefore means the candidate's name as
`scripts/analysis/eisv_ablation_matrix.py` prints it; two candidates are in
the same family only when they are the same candidate. Nothing else is
compared: not the feature a candidate reads, not its delta, not its
confidence interval, and not the set of candidates that were fitted.

**How it is applied.** The report quotes both cells verbatim and writes one
of `condition 4: met (<name> at both leads)` or
`condition 4: unmet (<name at lead 0> / <name at lead 30>)`. A `-` in either
cell (no candidate/baseline delta was formable on that slice) is `unmet`,
and the report writes `condition 4: unmet (- / <name>)` or
`condition 4: unmet (<name> / -)` or `condition 4: unmet (- / -)` as the
cells read. The comparison uses the registered read's output only; the
pre-declared sensitivity cohort, any exploratory or reproduction read, and
the output of any failed attempt play no part. Whether the two names happen
to share an EISV feature may be reported as context; it does not decide the
condition.

**Why this reading.** (1) The condition's own rationale names the argmax:
"an argmax that changes with a nuisance parameter is noise-mining." The
argmax is a candidate name, so the stability asked for is stability of that
name. (2) The pull request that registered this document (#1425, merged
2026-07-31) described the fourth condition as "a stable argmax across
leads"; this block restores that gloss. It is cited as the registering
text's contemporaneous wording, not as a pre-data warrant: the same pull
request body also reported the 2026-07-31 historical read. (3) It is
decidable from two printed cells with no mapping table, so no analyst
choice exists at read time, and the definition needs no knowledge of which
candidates exist or were fitted: it reads the same under any candidate
tuple. Whether the read may *run* under a changed tuple is a separate
question, answered by the pin described below. (4) It is the most
conservative reading available: every coarser partition admits winners the
rationale would call unstable.

**Candidate set at the time of this clarification, and the pin.** The
winner is drawn from `EISV_PRIOR_STATE_MODELS` in
`scripts/analysis/eisv_skeptic_report.py`, which at `master` `fb966bad`
names seven candidates:
`previous_bad_plus_prior_risk`, `prior_risk_binned`, `prior_phi_binned`,
`prior_s_binned`, `prior_verdict`, `prior_eisv_dispersion_binned`,
`previous_bad_plus_dispersion`. `global_bad_rate`, `previous_outcome_bad`
(the baseline) and `reported_confidence_raw` are scored but are not
candidates and cannot win. Selection among candidates is the lexicographic
key `(beats_baseline, auc_delta, brier_improvement)` in
`eisv_ablation_matrix.py`. `max` keeps the first maximal element, and the
candidates reach it in the order `build_model_scores` constructs them
(`prior_risk_binned`, `previous_bad_plus_prior_risk`,
`prior_eisv_dispersion_binned`, `previous_bad_plus_dispersion`,
`prior_phi_binned`, `prior_s_binned`, `prior_verdict`), not in the order
of `EISV_PRIOR_STATE_MODELS`, which `score_deltas_vs_baseline` uses only as
a membership filter. An exact tie on all three keys is therefore resolved
by construction order.

Alongside this clarification, a separately disclosed code change records
these seven names, as the tuple is written, and `DISPERSION_FEATURE = "prior_s_disp"`
on this protocol's entry in `REGISTERED_READ_MANIFEST`
(`eisv_ablation_matrix.py`), taken from `master` `fb966bad`. From that
change on, a `--read-protocol registered` read under this protocol's id (the
registered id and its `-retry-<n>` forms) compares the live
`eisv_skeptic_report` constants to the recorded values and refuses to read
if either differs. A difference is therefore adjudicated by the CLI as a
refusal to read; it is not disclosed and read through. The condition-4
rule above does not depend on the pin; the read does. What the pin
freezes, stated exactly: the candidate tuple as written and the dispersion
feature name. The comparison is exact tuple equality, so a reordering of
the tuple also refuses; that is a conservative choice of comparison, not a
claim that the tuple's order selects. What it does not freeze: the
tie-break order, which is the construction order in `build_model_scores`
and is not recorded anywhere, and what any candidate computes. The model
constructors in `build_model_scores`, their binning,
`min_feature_rows` (30), `MIN_DISPERSION_SNAPSHOTS` (5) and
`DISPERSION_WINDOW_MINUTES` (90.0) remain governed only by the registered
command's "run from a checkout of `master`". `score_deltas_vs_baseline`
binds its `candidate_names` default when the module is imported, so the
guard compares source constants: it detects a drifted checkout and is not
a runtime guarantee about what a read computes.

**Execution contract, strengthened.** The pin strengthens the registered
execution contract rather than disclosing something about it: the command
says "run from a checkout of `master`", and from the pin on, a `master`
whose candidate tuple or dispersion feature has moved cannot run this read
until the checkout is corrected or the manifest and this document are
amended by pull request. The operator attests this strengthening on merge
together with the clarification. Recovery path, so that a December refusal
is not ambiguous between "protocol violation" and "nothing happened yet":
the refusal is raised inside `validate_read_protocol`, which
`record_read_receipt` calls as its first statement, before the ledger
directory is created and before the receipt file is created with `O_EXCL`;
`main_async` calls `record_read_receipt` before `build_matrix_from_db`. A
read refused this way writes no receipt, consumes no read id, and touches
no database. The same `eisv-outcome-grounding-2026-12-01` id is then used
once the checkout is corrected or the manifest amended; the `-retry-<n>`
rule above governs failures after a receipt exists and is not triggered.
The refusal message names both the recorded and the live values; if a
refusal occurs, the December report discloses it.

**Disclosure of what was known when this was written.** The frozen
2026-08-09 descriptive matrix was public when this block was written. On
its two task-scope lead 0 / lead 30 pairs (30 d and 90 d windows, neither
the registered 365 d window) the winning names differ
(`prior_risk_binned` / `previous_bad_plus_dispersion` at 30 d;
`prior_risk_binned` / `prior_s_binned` at 90 d), so this definition would
have been unmet on every recorded pair. It is written with that fact in
view and is not presented as chosen in ignorance of it. It was written
before any access to the registered cohort; no live outcome read was
performed to prepare it. A coarser feature-axis partition was considered
and rejected (decision record:
`docs/proposals/active/open-decisions-packet-v0.md`, item D4; audit: #2154);
the adversarial design review of that packet split between the two
readings, and the operator selected winner-name identity. The feature-axis
partition would also have been unmet on every recorded pair, so the choice
between the two readings changed no recorded verdict.

**Smallest relevant effect.** The power-characterisation correction above
records that no beta, AUC delta, or equivalent effect size fills the
"predeclared smallest relevant effect" slot, and that the operator must
declare one before any further live outcome-discrimination access. The
operator's declaration, made 2026-09-23 before any access to the registered
cohort, is that **no smallest relevant effect is set for this read**. That
is a choice, not an omission, and it is recorded here as the declaration
the correction asks for. Three reasons, each checkable against this
repository: the record contains no relevance anchor for this estimand (every
candidate value in it is a detectability figure, a runtime report label, or
the withdrawn 0.05 bound that this gate bars by name); the only claim a
power-qualified `REFUTED` could refute is rework prediction, because the bad
class has never carried a violation, harm, or concealment row; and a value
chosen now so that `REFUTED` becomes reachable would be derived from what the
read can detect, which the gate forbids. Consequences: the December read
runs as registered; the operational stop rule decides PASS or FAIL
unchanged; on any non-PASS branch the scientific inference is `INCONCLUSIVE`
by declaration, exactly as the gate already provides; and `REFUTED` is
unreachable for this read. The slot is not closed for the future: a later
read under a new premise carries its own declaration. No agent-chosen value
is substituted. The wording of this declaration was drafted by the working
agent after an adversarial design review of the alternatives and adopted by
the operator, who ratifies it on merge.

**What this block does not do.** It does not alter conditions 1–3, the 150
block threshold, the 0.05 level, the 400-resample null, the cohort, the
fixture rule, the cutoff, the date, the command, or any estimator in
`eisv_ablation_matrix.py` or `eisv_skeptic_report.py`. It authorises no
read before 2026-12-01. It does not itself pin anything: recording the
candidate tuple and `DISPERSION_FEATURE` in
`REGISTERED_READ_MANIFEST` is a separate, disclosed code change made
alongside this clarification, and the rule above does not depend on it. It
does not change `independent-operator-cohort-preregistration-v0.md`'s
protocol; that document's "adopted verbatim" sentence is amended alongside
this clarification so its reading of "family" agrees with this one, while
its enrollment ledger is empty. It does not resolve the condition 1 →
condition 2 redundancy at 400 resamples (#2154 §2) or the agent-stratified
null question (#2154 §3).

**Attestation.** Attested by the operator on merge as a clarification of an
undefined term, not a weakening of the registered protocol (`CLAUDE.md`,
"Measurement authority — what a number may decide", exemption for
pre-registered scientific stop rules).

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
