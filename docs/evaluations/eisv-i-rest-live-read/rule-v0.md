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
- a pooled `approve` share of 413/453 (91.2%). Three roster residents carried
  most of it at 100% each.

No floor below is set from those numbers. The prediction is the one the
contract recorded before the window opened. The decisive statistic is the same
kind of pooled share, so the look was a look at that statistic. It was, however,
fleet-wide and mixed residents with ephemeral identities. This rule reads
strata, and every resident stratum is `INCONCLUSIVE` by construction (below).
So no figure that look produced can decide an outcome here.

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
  `2026-10-15T13:45:00Z` (A1's `ambiguous_from`). Rows must be non-synthetic.
  Stamping began with I1, so these rows carry no `eisv_instrument` stamp, and
  `is_settled` returns false for every one of them. They use the stamp-free
  equivalent of the same rule instead:
  - An identity first seen inside the era is settled from its first row.
  - An identity with rows before the era start is settled after its first
    `DEFAULT_SETTLED_UPDATES` (50) rows in the era. That mirrors `carried_from`
    and `updates_since_change` for stamped rows.
  - A row in this era that does carry a stamp fails the read as `INCONCLUSIVE`,
    since stamping did not exist in this era.
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
- **the decisive statistic:** the pooled row-level `approve` share, meaning
  approve rows over safe rows in the stratum. This is the statistic contract
  row 50's endpoints are computed in.
- beside it, with no outcome of its own:
  - the p50 of per-identity `approve` shares over identities with at least 10
    safe rows, with its n;
  - the pre → post shift of the pooled share, per stratum present in both eras.

## Floors

A stratum below any floor is `INCONCLUSIVE` for that stratum. Each floor
carries its referent.

| Floor | Value | Referent |
|---|---|---|
| Identities with at least 10 safe rows | ≥ 5 | The skill-recorded floor for a fleet-level reading on one instrument: at least 5 genuinely distinct agents (`unitares-eisv-maths`, individuality). |
| Safe rows in the stratum | ≥ 100 | Gives a binomial 95% half-width of at most about 10 points, a quarter of the 40-point predicted range. |
| Safe rows per counted identity | ≥ 10 | A per-identity share moves in steps of at most 10 points. |
| Largest single identity's share of the stratum's safe rows | ≤ 50% | A majority: above it, the pooled share describes that identity more than the stratum. |

The review's look saw one identity writing 55% of all behavioral rows. That
identity is a roster resident, so it falls in its own stratum, and the cap does
not apply to it there.

**Consequence: every roster-resident stratum is `INCONCLUSIVE` by
construction.** A resident is its own class, so its stratum has one identity,
which fails the five-identity floor. A resident's own share is still reported,
as one identity's figure, not as a fleet-level reading.

## Outcomes, per stratum

- **`INCONCLUSIVE`:** a floor is unmet, the window crosses an era boundary,
  stamped ids are mixed, or the stratum is empty in either era.
- **`CONSISTENT`:** the post-era pooled share lies in [44.1%, 84.5%].
- **`BELOW`:** the post-era pooled share is under 44.1%. This is informative.
  The repairs moved the action mix less than their own estimate.
- **`ABOVE`:** the post-era pooled share is over 84.5%. It is reported with one
  of three qualifiers:
  - `composition`, when the stratum's baseline-era pooled share already exceeded the
    deployed endpoints (26.7% and 28.9%). The stratum's mix then differs from
    the replay's.
  - `loop-consistent`, when behavioral E rose across the window for the
    identities counted. This is the `decision_e` path: in contract row 45,
    `approve` scores 1.0 against `guide`'s 0.7.
  - `unattributed`, otherwise.

The range is checked against the post-era level. The pre → post shift is
reported beside it and has no outcome of its own: the replay's deployed
endpoints (26.7%, 28.9%) were computed on a different mix than either live era.

The pooled range matches the read's statistic but not its composition. The
endpoints were computed over the whole 209-identity mix, not per stratum.
Per-stratum endpoints can be recomputed offline from the frozen 14-day export
(label-free; no outcome access). They must use the same statistic (pooled share
over the stratum's safe rows) and the same row filters. If they exist by the
read, each stratum is checked against its own endpoints instead of the pooled
range, and the report says which was used. If not, the read is labelled
**range-only**, and a `BELOW` or `ABOVE` from it means "outside the fleet-mix
estimate", not "off this stratum's estimate".

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
