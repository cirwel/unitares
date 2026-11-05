# EISV Decision Records

**Status:** Historical decision record companion to [`eisv-proprioception-contract.md`](eisv-proprioception-contract.md)
**Last Updated:** November 4, 2026

> **Scope:** Full provenance and rollout records for the August–September 2026
> EISV governance decisions summarized in [`eisv-proprioception-contract.md`](eisv-proprioception-contract.md).

---

## Decision record — legacy C(V) is control feedback, not health evidence (2026-08-11)

Historical reconstruction separated three objects that runtime vocabulary had
collapsed into “coherence”: directional ODE control feedback `C(V)`, update-direction
consistency `rho`, and an external state-coherence readout. The deployed legacy
function

```text
C(V) = Cmax * 0.5 * (1 + tanh(C1 * V))
```

is monotone in signed imbalance, equals `Cmax/2` at balance, and satisfies
`C(-V)=Cmax-C(V)`. It therefore encodes controller direction/activation: positive V
is running hot and negative V is running careful. It is not a symmetric score whose
higher values mean healthier balance.

The compatibility field and `coherence()` import remain because they are embedded in
stored and public contracts. New ODE code names the producer `control_feedback`;
new persisted rows identify the winning instrument in `state_json.coherence_form`,
and response metrics carry the additive `coherence_source` / `coherence_role`
pair. The flag-gated grounding function runs only in the explicit pre-persist
stage; it is no longer also registered as a late response enrichment that could
silently change what downstream enrichments observed.
No live gate or threshold changes in this correction: replacing the frozen legacy
field with behavioral-V or manifold coherence would move previously unreachable
branches and requires prospective calibration first.

The legacy scalar also has an indirect verdict path that the old “ODE is only
parallel telemetry” description hid: `behavioral_sensor.py` assigns its level
25–30% of E and its trend 30–40% of I. That coupling was added to differentiate
the sensor before the April behavioral migration demoted ODE E/I/S/V but omitted
coherence. Removing it changes behavioral baselines and verdict inputs, so this
correction makes its provenance explicit in the EISV derivation envelope and
documentation but does not silently reweight it. A prospective deconfounding
shadow is the next prerequisite.

The same legacy scalar retains 55% of the base weight in the omitted-confidence
fallback (`src/confidence.py`). That makes derived confidence another mixed-
provenance consumer, and confidence history can feed a later calibration penalty
on ODE entropy. New response metadata exposes this causal dependency; removal or
reweighting belongs in the same prospective deconfounding program rather than a
silent compatibility change.

The shadow comparison is likewise measurement-only. Its corrected v2 statistic is
two-sided, uses a bounded recent window, excludes the current observation from its
own reference, and states whether empirical dispersion or a calibrated scale floor
supplied the denominator. Its comparison label is independently versioned and
attributes the fleet result from `sub_action` + `nearest_edge`; an unrelated risk,
void, or basin pause is not counted as a coherence-gate agreement. Monotonicity
alone is no longer treated as health semantics.

## Decision record — retire legacy coherence authority outside the shadowed actuator (2026-08-12)

Once producer provenance established that the deployed scalar is legacy C(V),
non-actuator consumers no longer needed outcome evidence to stop making an
invalid category claim. Recovery eligibility and margin, stuck recovery,
dialectic triage, peer similarity/outlier ranking, state-announcement filtering,
focus stability, significance logging, anomaly/trend classification, and
behavioral-baseline anomaly entropy now exclude legacy C(V) from authority.
Knowledge-write confidence and auto-outcome quality likewise no longer inherit
their values from that controller, and synthetic dialectic reviewers receive it
as provenance-tagged diagnostic context rather than independent evidence.
Automatic ground-truth evaluation no longer substitutes it when an audit row's
actual confidence is missing; absence stays absent instead of becoming invented
calibration evidence. New dialectic coherence conditions are not installed, and
stored unprovenanced `coherence_target` conditions retire in place without
escalating a decision.
Compatibility responses may still display or retain the value for replay, but
identify its source/role and mark it non-authoritative.

The check-in actuator is a different migration boundary. The configured
coherence pause, CIRS, basin, and adaptive-governor paths remain deployed while
their versioned gate shadow collects prospective evidence; changing those paths
would alter interventions and learned baselines. The coherence-derived
ethical-drift/ΔEta path likewise remains policy-stable, but needs its own
measurement-only ablation before migration. Trajectory identity remains
deployed while
`legacy_coherence_identity_ablation.v1` records the coherence-only
recovery/stability dependency without writing candidate signatures or changing
trust and risk policy.

## Decision record — cold-start confirmation remains shadow-only (2026-08-10)

Trailing-90-day inspection found 66,890 non-synthetic state rows, including 1,420
rows below behavioral confidence 0.3. Five of those fallback rows produced
`risk_pause`; none was followed by another fallback `risk_pause`, and four have a
matching circuit-breaker audit event. These observations establish delivery and
non-persistence in the sample. They do **not** establish false positives, avoided
harm, or a safe suppression policy.

Independent dialectic review (`8539c516649a08af`) rejected default-on actuation:
the verification floor is itself default-off, and process-local confirmation could
repeatedly reset across restart or hydration uncertainty. The accepted revision is
therefore instrumentation-first:

1. Evaluate only an original risk-routed pause whose complete decision-time
   hard-stop record proves risk was the sole effective stop, verdict source is
   exactly `phi_cold_start`, primary EISV source is exactly `ode_fallback`,
   confidence is finite and below 0.3, monitor lineage is identity genesis, and
   no independent override fired.
2. Record first adjacent evidence as `shadow_would_defer` and the second as
   `shadow_confirmed`; reset on intervening decisions, source/readiness changes,
   history gaps, restart, hydration uncertainty, or lineage discontinuity.
3. Preserve the original action/reason and expose readiness, source, confirmation
   count, reset/ineligibility reason, counterfactual outcome, and enforcement basis
   in policy, enforcement, persisted telemetry, audit, and fleet health.
4. Keep actuation default-off and fail-closed even if its operator flag is set,
   until confirmation state is durably and atomically persisted and prospective
   evidence receives explicit operator review.

This shadow record unveils the policy-to-actuator edge without changing a single
pause. It is evidence for a later decision, not that decision itself.

The word *actuation* has two scopes on this record and they must not be compared
as if they were the same switch:

- `policy_evaluation.maturity_gate.actuation_*` describes only the dormant
  **confirmation deferral** that could convert a first fallback-owned pause into
  guidance. Its scope is now emitted as `fallback_risk_pause_deferral`.
- `enforcement.*` describes the existing authenticated **runtime circuit
  breaker**. An underlying proven-risk-only pause (`risk_pause`, or a CIRS
  `cirs_block` with complete hard-stop provenance) can therefore have confirmation
  `actuation_applied=false` while runtime `enforcement.applied=true`; that means
  the shadow did not alter the policy and the pre-existing pause was delivered.

Every newly delivered runtime circuit breaker carries one `actuation_id` and
`applied_at` across the response, persisted EISV envelope, lifecycle metadata,
and broadcast payload. Recovery diagnostics report the observed enforcement
facts separately from whether they match a narrow recovery exception. A failed
exception predicate must never be presented as evidence that actuation did not
occur.

## Decision record — non-authored cold-start authority guard (2026-08-10; amended 2026-08-22)

Prospective evidence after the confirmation shadow shipped exposed a narrower
fault than the dormant two-observation proposal addresses. The first three
`shadow_would_defer` events were `substrate_interpretation` rows rather than agent
reports; all three nevertheless delivered circuit-breaker pauses and began from
the same Phi-fallback risk (`0.7858974`). A fourth fresh-process reproduction
during implementation also auto-paused on a `substrate_interpretation` before its
first authored `sync_state`; its exact persisted row satisfies the same recovery
predicate. One of the fresh identities then could not recover: every
reviewed attempt re-read the same frozen risk while the pause gate prevented the
identity from writing the observation that could mature the estimator.

The deployed correction is an epistemic-authority rule, not promotion of the
confirmation actuator:

1. With `GOVERNANCE_NON_AUTHORED_COLD_START_GUARD=true` (default), downgrade only
   an original fallback-risk pause: either `pause/risk_pause`, or
   `pause/cirs_block` with `nearest_edge=risk`. The route label is necessary but
   insufficient. A versioned decision-time record must prove complete inputs and
   thresholds, at least one risk hard stop, and no resonance, CIRS/configured
   coherence floor, void, independent low basin under a risk-neutral
   counterfactual, independent verification/operator override, or unclassified
   cause. The row must also be non-agent-authored, verdict source exactly
   `phi_cold_start`, primary EISV source exactly `ode_fallback`, behavioral
   confidence finite and below 0.3, and readiness explicitly false.
2. Preserve the original pause, reason, selected edge, complete trigger set,
   observed inputs, and thresholds in decision history, audit, and the versioned
   `epistemic_gate`; send `proceed/guide` with
   `enforcement.requested=false` to the authenticated runtime boundary.
3. Leave agent-authored reports, behaviorally ready evidence, independent
   verification, void/coherence/basin gates, CIRS oscillation/coherence/unknown
   edges, loop detection, and every other pause source unchanged. Missing or
   contradictory provenance fails closed.
4. For identities already trapped by the legacy behavior, reviewed recovery may
   discount the frozen risk check only when the latest persisted telemetry proves
   the exact non-authored `shadow_would_defer` circuit breaker **and** contains the
   same complete, internally consistent risk-only hard-stop record. Older rows
   that lack those decision-time inputs cannot be reconstructed after the fact.
   Ownership, reflection, no-void, and persistence checks still apply. Legacy `C(V)` is
   diagnostic context, not recovery authority. Quick recovery retains its
   stricter risk/no-void contract and cannot use the cold-start risk exception.

The policy-vocabulary boundary is exhaustive:

| Action | Sub-action | `nearest_edge` | Hard-stop provenance | Fallback-risk candidate |
|---|---|---|---|---|
| `pause` | `risk_pause` | `risk` | complete and risk-only | yes |
| `pause` | `cirs_block` | exactly `risk` | complete and risk-only | yes |
| `pause` | either risk route | any | missing, contradictory, unclassified, or has any independent stop | no |
| `pause` | `cirs_block` | `oscillation`, `coherence`, absent, or any other value | any | no |
| `pause` | anything else | any | any | no |
| anything else | any | any | any | no |

The 2026-08-22 amendment closes a vocabulary bypass found in #1817 while also
closing the converse safety hole identified by independent dialectic review
(`196acc3739ea0008`): `nearest_edge=risk` is only the selected explanation and
can mask simultaneous hard stops because the policy stack returns early. CIRS
v2 now records its actual 0.25 coherence floor, 0.80 risk ceiling, oscillation
inputs, and resonance thresholds; `monitor_decision` combines them with the void,
configured-coherence, verdict, and basin conditions before selecting a route.
The guard validates that record field-by-field instead of trusting its aggregate
`risk_only` bit. Historical recovery remains the exact
`risk_pause`/`shadow_would_defer` exception above only when the persisted row has
the full record; this change does not reinterpret old `cirs_block` rows or infer
missing historical inputs.

The separate two-confirmation actuator remains dormant under dialectic decision
`8539c516649a08af`. The new guard has no counter, does not trust a second fallback
observation, and does not weaken independently grounded safety evidence. Rollback
sets `GOVERNANCE_NON_AUTHORED_COLD_START_GUARD=false` and restarts the service.

**Amendment 2026-09-24 — agent-authored reports.** With
`GOVERNANCE_COLD_START_GUARD_INCLUDE_AUTHORED=true` (default), rule 1's
"non-agent-authored" condition is lifted: an agent's own report whose verdict is
owned by the same Phi cold-start fallback is downgraded under exactly the same
risk-only, provenance, maturity and no-independent-override conditions, and is
recorded with its own enforcement basis (`authored_phi_cold_start_deferred`).
Every other boundary in rule 3 stands. Evidence (dated window measurements on
the live database, 2026-09-24):

- From 2026-08-30 13:24 (when `auto_attest` first carries behavioral confidence)
  to 2026-09-24, 0 of ~19,300 `auto_attest` rows at behavioral confidence >= 0.3
  produced a pause verdict; every pause verdict in that window came from the
  cold-start prior.
- The 249 pause verdicts from 2026-08-24 to 2026-09-24 all carried a reported
  complexity of 0.80–0.94, against a text-derived complexity of 0.14–0.31
  (median 0.16). 241 of them came from automatic check-ins
  (`substrate_interpretation`), not agents' own reports. 123 identities
  received the identical risk 0.7859. The inputs are a reported complexity and
  a complexity derived from a short template; see #2418.
- The one pause delivered in the thirty days to 2026-09-24 was an agent's first
  authored report, at behavioral confidence 0.2, whose behavioral assessment
  read `safe`.
- In the same thirty days the confirmation shadow requested by
  `8539c516649a08af` saw 7 authored cold-start candidates: 6 ineligible
  (`history_gap`) and 1 `shadow_confirmed` (the delivered pause, where a
  preceding hook row counted as confirmation 1). It would have deferred none.
  This amendment is not that counter and does not promote it.

What this does not establish: that cold-start pauses were false positives, or
that removing them avoids or allows harm. What it changes: a risk-only verdict
from the prior, with complete provenance, no longer actuates a hard stop by
itself. The prior can still pause through the fail-closed paths (missing or
contradictory provenance) and through a risk-derived low basin (`basin_pause`),
which is not a risk route. Structural and runtime-safety stops (void,
coherence floor, CIRS resonance, loop detection) and independent verification
still pause. Rule 4's recovery exception is not extended to authored rows; an
authored pause delivered before this change cannot use it (none was in force
on 2026-09-24). Rollback sets `GOVERNANCE_COLD_START_GUARD_INCLUDE_AUTHORED=false`
and restarts the service.

## Decision record — V-reintroduction deferred (2026-08-07)

The grounded-coherence re-derivation proposal
(`docs/proposals/archive/eisv-grounded-coherence-rederivation-v0.md`) drops V from the
certified core and leaves reintroduction — as a policy-layer leaky EMA of the
outcome-channel residual — as an open design decision. That decision was taken
through a governed dialectic review (session `3e003d82fb2d251e`, resolved
converged 2026-08-07): **defer, with a wired wake condition — neither build now
nor retire permanently.** Conditions, binding on any future V work:

1. V stays out of the certified core and off all governed/verdict surfaces. The
   2026-12-01 outcome-label stop-rule read (#1425) is the calendar **trigger
   only** — necessary, not sufficient; the frozen #1425 pre-registration is not
   amended to carry V-specific criteria.
2. Any V-EMA build requires its own prior pre-registration defining (a) evidence
   minima — per-agent and per-independent-cluster effective samples, temporal
   cadence, externally corroborated labels, settlement examples, and a
   missingness/selection-bias audit (label *quality*, not raw volume); (b) the
   EMA maths — residual definition, prior, decay/half-life, accrual **and
   settlement** equations; (c) an incremental-information test against E, I, S
   and naive baselines that V must pass to count as an independent axis.
   Exogenous input is not, by itself, proof of independence.
3. If built: the soak must clear **both** ≥30 days **and** the pre-registered
   evidence minimum, tagged advisory with zero verdict weight, with explicit
   rollback criteria and demonstrated settlement (recovery) behaviour. Any later
   gate/hysteresis/verdict-weight use is a separate review.
4. At implementation of the 3-state certified core, CI tests must prove
   V-absence from verdict, pause, risk, coherence, gating, and hysteresis paths.

**Consumer inventory — where V feeds the deployed system today** (the surfaces
condition 4's tests must cover; dev tree `4bc9d4af`):

| Surface | Mechanism |
|---|---|
| `governance_core/coherence.py` | deployed `coherence` field is the legacy-named directional `control_feedback(V)` — pinned ≈0.49 by near-zero ODE V |
| `governance_core/dynamics.py` | V feeds back into the ODE through the directional control-feedback term |
| `governance_core/stability.py:141` | `dC/dV` enters the contraction certificate (the deployed α_c = 0.019 is certified over the 4-coordinate system) |
| `src/grounding/coherence.py` | intended grounded readout is a **V-free** manifold distance over (E,I,S), shadowed by default and canonical only under APPLY |
| `src/behavioral_sensor.py:203` | behavioral V = E−I slope difference (the ~0.99-corr estimator; ledger row 1) |
| `dashboard/redesign/{data,snapshot}.js` | display surfaces render V as a first-class axis |

The 4-axis ontology remains at the paper/vocabulary level throughout the
deferral; this record exists so no future session re-derives the question.
