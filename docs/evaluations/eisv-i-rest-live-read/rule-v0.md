# Live read of the 2026-10-15 basin and I-rest repairs: decision rule v0

**Status:** decision rule, declared 2026-10-16, before the read it governs.
The read is due on or after 2026-10-23. The rule sets telemetry thresholds
only. It carries no removal or enforcement authority, so it sits in the agents'
column of `CLAUDE.md`'s authority table. Nothing it reports changes a
threshold, a flag, or the 2026-12-01 registered read. That read's data boundary
ends at `instrument_change_at` (2026-10-15T13:45Z), before every row this rule
reads.

**Not an outcome read.** It joins no outcome labels and makes no claim of
benefit. It checks whether the live action mix moved the way the repairs'
offline estimate said it would.

**Prior look, disclosed.** The rule was written about 21 hours into the window.
During the adversarial design review that produced it, a live check read raw
counts for that span. It found:

- 453 behavioral `verdict = safe` rows across 11 identities;
- 4 identities writing 89% of those rows;
- a row-level `approve` share.

No floor below is set from those numbers. The prediction used is the one the
contract recorded before the window opened. The decisive statistic is
identity-level, rather than the row-level share that was seen.

## The prediction being checked

Contract row 50 (`docs/ontology/eisv-proprioception-contract.md`, "Expected
effect") estimated the change from an equilibrium shift over the frozen 14-day
export to 2026-10-15. Over n = 9,088 rows and 209 identities, behavioral
`safe` → `approve`, deployed → repaired, moved:

- 26.7% → 44.1% with the Φ fast-trip applied to every row;
- 28.9% → 84.5% with the fast-trip removed from every row.

Row 52's repair (era A1) removes the fast-trip where the behavioral assessment
owns the verdict. So the live figure is expected between the two endpoints.

## Window

- **Post-repair era:** RK1, from `2026-10-16T02:59:50Z` to the read time.
  RK1 changes no categorical output (its golden summary), so the action mix it
  inherits is the A1 + I1 build's.
  - Rows must carry one stamped instrument id (`RK1:a2579740d3bea62a`), checked
    with `require_single_instrument`.
  - Rows must be settled (`is_settled`) and non-synthetic.
  - If a later change is registered in `config/eisv_instrument_eras.json` first,
    the window ends at its earliest boundary (`ambiguous_from` when present).
- **Pre-repair baseline era:** ID, from `2026-10-05T22:06:59Z` to
  `2026-10-15T13:45:00Z` (A1's `ambiguous_from`). The same filters apply.
- **Read the two eras separately, never pooled.** The comparison is between
  two separate reads, one per era.

## Population and strata

- **Behavioral only.** `state_json.sensor_eisv_source = 'behavioral'`.
  - Physical and unrecorded rows are counted and reported, never compared with
    the range, and never pooled with behavioral.
  - The physical class is one identity, so nothing about it is decided here.
- **Denominator and numerator.** The denominator is rows with
  `state_json.verdict = 'safe'`, the contract's replay denominator. The
  numerator is the subset with `action = 'approve'`.
- **Verdict driver.** Rows are stratified by
  `state_json.eisv_telemetry.policy_evaluation.inputs.verdict_source`:
  `behavioral_assessment` against
  every other value. The two replay endpoints bracket these two strata. They
  were computed over all rows, though, not per stratum.
- **Identity class.** Rows are also stratified by
  `classify_by_label_and_tags` (`src/grounding/class_indicator.py`): one class
  per roster resident, then `engaged_ephemeral` and `ephemeral`. The reason is
  composition. The replay covered 209 identities over 14 days, while a short
  live window is dominated by a few residents. A fleet-wide rate across
  different mixes is a mixture, not a level.

## Statistics reported, per era and per stratum

- safe rows, identities, and p50 safe rows per identity;
- the largest single identity's share of rows;
- the row-level `approve` share;
- **the decisive statistic:** the p50 of per-identity `approve` shares, over
  identities with at least 10 safe rows in the stratum, with its n;
- the pre → post shift of that p50, per stratum present in both eras.

## Floors

A stratum below any floor is `INCONCLUSIVE` for that stratum. Each floor
carries its referent.

| Floor | Value | Referent |
|---|---|---|
| Identities with at least 10 safe rows | ≥ 5 | The skill-recorded floor for a fleet-level reading on one instrument: at least 5 genuinely distinct agents (`unitares-eisv-maths`, individuality). |
| Safe rows in the stratum | ≥ 100 | Gives a binomial 95% half-width of at most about 10 points, a quarter of the 40-point predicted range. |
| Safe rows per counted identity | ≥ 10 | A per-identity share moves in steps of at most 10 points. |

There is no cap on a single identity's share. The identity-level p50 is what
decides, so one heavy identity cannot carry it, and a cap chosen now would be
chosen after seeing a 55% share.

## Outcomes, per stratum

- **`INCONCLUSIVE`:** a floor is unmet, the window crosses an era boundary,
  stamped ids are mixed, or the stratum is empty in either era.
- **`CONSISTENT`:** the post-era p50 lies in [44.1%, 84.5%].
- **`BELOW`:** the post-era p50 is under 44.1%. This is informative. The
  repairs moved the action mix less than their own estimate.
- **`ABOVE`:** the post-era p50 is over 84.5%. It is reported with one of
  three qualifiers:
  - `composition`, when the stratum's baseline-era p50 already exceeded the
    deployed endpoints (26.7% and 28.9%). The stratum's mix then differs from
    the replay's.
  - `loop-consistent`, when behavioral E rose across the window for the
    identities counted. This is the `decision_e` path: in contract row 45,
    `approve` scores 1.0 against `guide`'s 0.7.
  - `unattributed`, otherwise.

The range is checked against the post-era level. The pre → post shift is
reported beside it and has no outcome of its own: the replay's deployed
endpoints (26.7%, 28.9%) were computed on a different mix than either live era.

Per-stratum endpoints can be recomputed offline from the frozen 14-day export
(label-free; no outcome access). If they exist by the read, each stratum is
checked against its own endpoint instead of the pooled range, and the report
says which was used. If not, the read is labelled **range-only**.

## What this read does not decide

- **The shadow promotions.** The coherence gate and grounding repair are a
  separate standard.
- **Lumen ODE anchoring.** Under `UNITARES_SENSOR_COUPLING=behavioral_only` the
  physical ODE has no anchor. On 2026-10-15 Lumen's sensor I was 0.846 while its
  ODE I was 0.66 (contract row 50, "Still open"). Anchoring it changes the live
  path for a one-identity class. That makes it a mechanism decision reserved to
  the operator, informed by an offline anchored replay, not by this behavioral
  read.
- **Anything about outcomes, harm, or agent quality.**
