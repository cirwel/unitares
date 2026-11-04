# EISV Proprioception Contract

**Created:** June 26, 2026
**Last Updated:** November 4, 2026
**Status:** Active

> **Scope:** This is the contract for EISV, one state-estimation instrument
> within UNITARES, not a definition of the product. For what UNITARES is, see
> [`PRODUCT_DEFINITION.md`](../PRODUCT_DEFINITION.md).

---

## Canonical operational doctrine

> **Digital proprioception senses. EISV estimates state. Autonomic governance regulates.**

This is the canonical operational reading of EISV.

**Digital proprioception is the sensing layer.** It names the runtime observations
that make an agent/process legible to itself and to the surrounding governance
system. The term does not imply subjective experience, biological equivalence,
or independent volition.

**EISV is the state-estimation layer.** It turns those observations into a
structured estimate of runtime state for autonomic regulation. The
[operational definitions](../EISV_COMPUTATION.md#what-the-signals-record)
identify what each implemented coordinate contains, including controller
history and derived signals. Recording those signals, interpreting them as a
diagnosis, and choosing a policy response are separate questions. A narrow or
derived signal remains inspectable information; comparative benefit is evaluated
for a specified consumer and task in the [claim ledger](../EVIDENCE_AND_LIMITS.md#claim-ledger).

**Autonomic governance is the regulation layer.** Policy, review, identity,
authority, outcomes, and enforcement may consume EISV state estimates, but an
EISV reading does not by itself authorize intervention. Measurement, state
estimation, verdict formation, policy, and enforcement remain distinct
surfaces.

The stronger biological, thermodynamic, allostatic, free-energy, and related
interpretations in the repository are research interpretations or historical
lineage unless separately supported by evidence. UNITARES does not require
those interpretations to be literally true for the operational EISV contract
above to hold.

When describing EISV, distinguish four epistemic levels rather than collapsing
them:

1. **Operational / deployed** — what the runtime currently computes or uses.
2. **Measured / empirical** — what an experiment or dated observation supports.
3. **Research interpretation** — a model or explanatory framing under study.
4. **Hypothetical / analogy** — a useful comparison that is not itself evidence.

Do not promote a claim upward across those levels without new evidence. In
particular, "EISV enables autonomic agents" is not an established claim; the
architectural-role statement above is the one to use.

## Contract

**EISV serves a proprioceptive engineering role**: it is a runtime self-state
estimate about agent strain, coherence, entropy, integrity, and imbalance. It is the system saying "my balance
is changing" or "this process is running hot," not a court deciding whether a
worker was morally bad.

EISV is **not an outcome oracle**, **not a grand jury**, and **not a
bad-verdict dispenser**. It does not decide whether harm occurred, whether the
user was wronged, or whether an agent is "guilty." Those judgments require
external outcome evidence, policy, and review surfaces that are separate from
the measurement vector.

"Proprioception" here is the anti-verdict claim, and it holds. It is not a claim
that all four axes share one sensory class: E incorporates externally referenced
signals; I and S primarily summarize internally or self-referenced runtime
information; V is derived from other state dimensions rather than receiving an
independent input. Because those classes imply
different validation regimes, see **Sensory class split** below before designing
or scheduling any EISV validation work.

### What the word does NOT claim — added 2026-08-18, operator-identified

The term is retained deliberately: it earns its place as the anti-verdict claim
above, and the published papers use it. Three things it must never be read as
asserting.

**1. No interiority.** "Proprioception" names a *self-state estimate*. It is not
a claim that anything is experienced, felt, or wanted. The system is artificial,
and that is the category, not a shortfall measured against an organic baseline.
⛔This matters because borrowed biological vocabulary generates the wrong
measurements and then the wrong research questions: the retired
`voluntary_kg_retrieval` KPI presupposed a volition baseline in which an agent
would-or-would-not call a tool absent context. No such baseline exists — every
call is conditioned on context the operator controls — so its zero measured what
was injected, not what agents valued. Renamed to `agent_kg_retrieval` 2026-08-18.
Before adding any metric, check that its name does not presuppose a state the
system cannot have.

**2. ⛔The sample is SCHEDULE-selected, not behavior-selected.** Proprioception
in an organism is continuous and event-driven. Here, *when* an observation exists
is a property of a plist. Verified 2026-08-18 from `~/Library/LaunchAgents` and
`core.agent_state`:

| Producer | Cadence | Source |
|---|---|---|
| Vigil | `StartInterval=1800` (30 min) | `com.unitares.vigil.plist` |
| Chronicler | `StartInterval=86400` (daily) | `com.unitares.chronicler.plist` |
| Sentinel (BEAM) | `KeepAlive=true` (continuous) | `com.unitares.sentinel-beam.plist` |
| Dialectic canary | `StartCalendarInterval` 08:40 | `com.unitares.dialectic-canary.plist` |

**Scheduled residents emitted 86.8% of all check-ins over the 30 days to
2026-08-18** (26,333 of 30,331 `core.agent_state` rows — a dated window
measurement, not a standing total; re-measure before citing). The *content* of
each check-in is model-authored and does vary; the *existence* of the row is a
timer firing.

Consequence for validation: any distributional claim over this corpus inherits
the schedule as a confound. A rate, a base rate, a drift, or an AUC computed on
it is conditioned on cadence, not on agent behavior, and changing a plist
interval would move it without anything about agent behavior changing.
⛔Do not describe such a result as characterizing "the fleet" or "agent
behavior" without stating the cadence weighting. This is a limitation of the
sample, not of the instrument, and it is not addressed by measuring harder.
See also the deployed-vs-target separation below: this is the same discipline
applied to the sampling frame rather than to enforcement.

**3. ⛔The class constants are AGENT-selected, not just observation-selected.**
Clause 2 is about which rows exist. This one is about who produced them.

The class-conditional calibration in `config/governance_config.py`
(`DELTA_NORM_MAX_BY_CLASS`, `HEALTHY_OPERATING_POINT_BY_CLASS`) is keyed on
generic behaviour classes and was regenerated with an empty roster, so no
resident is named. But clearing the roster only *renames* the data — it does
not broaden it. Recounted 2026-08-19 over the original 2026-05-28..06-27
window:

| class | rows | distinct agents | scope |
|---|---|---|---|
| `embodied` | 12501 | **1** | single_agent |
| `resident_persistent` | 8406 | **4** | single_principal |
| `engaged_ephemeral` + `ephemeral` | 2392 | 418 (family total, split unattributed) | unrecorded |
| `default` (fleet fallback) | 2033, measured 2026-04-18 | unknown | unrecorded |

**`embodied` is one device.** Its healthy operating point (E≈0.32) is a
well-measured fact about that device and a *hypothesis* about the class. The
prior wording — "embodied/edge agents genuinely run low-energy; that is their
normal" — stated it as a class property, which is a generalization from n=1.

Note the inversion: the classes with the most rows have the fewest agents, and
the class thousands of adopter agents actually land in is the thinnest and the
stalest. A large `corpus_size` is not breadth.

**Multi-principal consequence.** The commitment is per-principal governance
with no shared administrative root. A constant measured on one deployment and shipped
as a class-wide norm *is* a shared administrative root for what counts as
healthy — the homogenization failure one layer up, which is exactly what
multi-principal governance exists to prevent.
So `ScaleConstant` now records `distinct_agents` / `distinct_principals` and
derives `population_scope`; a `single_agent` constant reports
`federation_exportable = False`. Unrecorded breadth is also not exportable —
absence of evidence is not evidence of breadth.

⛔This is a DISCLOSURE change, not a re-measurement. No constant's value moved
and no class's fallback changed; the numbers are not wrong, the claim attached to
them was broader than the data.

**Corrected citation.** An earlier revision of this paragraph named the
pre-registered outcome-grounding stop rule (#1425) as forbidding the re-fit. That
citation is withdrawn. #1425
([stop rule](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)) is scoped to
outcome-label discrimination — whether EISV separates good outcomes from bad — and
constrains re-runs of the ablation probe. These anchors are fitted on a label-free
slice: a median and a p95 over `HEALTHY_REGIMES`, consuming no outcome label. *What
this changes*, below, already states that scope correctly; this paragraph did not.
The two now agree.

Two grounds do hold, on their own terms:

1. **Methodological, and currently unregistered.** Re-fitting a constant *because*
   its narrowness was just discovered chooses the analysis after seeing the data.
   That is the discipline #1425 encodes, applied to a different object — which makes
   it an analogous rule, not that rule. It needs its own registration rather than
   #1425's authority.
2. **Empirical, and decisive for the thin classes.** `default` cannot be re-fit at
   any staleness threshold: its 2026-06-27 slice held N=16 against the generator's
   own `--n-min 30` (recorded on `DELTA_NORM_MAX[default]` in
   `config/governance_config.py`). Sub-threshold classes are emitted as a comment,
   so re-running the generator would *remove* `default` and route every
   unclassified agent to the `DELTA_NORM_MAX_DEFAULT` placeholder — a value tuned
   for a geometric property, not measured. Here re-measurement is not available;
   it is a deletion.

## Deployed posture — corrected 2026-08-10

**As deployed, UNITARES is primarily advisory, but its authenticated circuit
breaker is live.** The stronger 2026-08-06 claim that no governed pause was being
delivered was falsified on 2026-08-09, when a fallback-owned ``risk_pause`` at
behavioral confidence 0.1 reached ``lifecycle_paused``. Measurement, policy, and
enforcement must therefore remain separate in every surface; neither "all pauses
enforce" nor "no pauses enforce" is true.

Concretely, and verified live:

- **Pause / high-risk verdicts are often, but not always, suppressed.** The
  2026-08-06 audit snapshot found `gap_suppress` downgraded 195 of 218 recorded
  pauses (89.4%) when inter-check-in gaps crossed 150s. A first check-in shortly
  after monitor creation can bypass that cadence gate; the 2026-08-09 delivery did.
- **The latest verified governed delivery is 2026-08-09, not 2026-06-28.** It was
  a Phi cold-start decision, not a behavioral-authoritative verdict. Its next
  persisted observation approved rather than repeating the pause. That is an
  observed non-persistent sequence, not proof the pause was false.
- **A produced verdict is not an enforced action.** `lifecycle_paused` events are
  delivered enforcement; `auto_attest` rows with `decision='pause'` /
  `gap_suppressed='true'` are produced verdicts that did **not** enforce. Never read
  a pause / high-risk verdict *count* as an enforcement count. Operator-facing
  surfaces that label a produced verdict `critical`, `paused`, `halted`, or
  `enforced` are mislabeled against this posture and are being corrected to advisory
  vocabulary (`guide` / `advisory` / `would-pause`).

The proprioceptive instrument, guidance, audit record, and circuit breaker are all
real. A surface may say an agent was halted only when the same observation records
`enforcement.applied=true` (or a matching lifecycle event); a produced pause alone
is insufficient.

## Decision record — legacy C(V) is control feedback, not health evidence (2026-08-11)

The deployed legacy function `C(V) = Cmax * 0.5 * (1 + tanh(C1 * V))` encodes
directional ODE control feedback (positive V is running hot, negative V is
running careful), not a symmetric health score. Persisted rows identify the
winning instrument in `state_json.coherence_form` and response metrics carry
`coherence_source` / `coherence_role`. No live gate or threshold changes
without prospective calibration, and indirect couplings in
`behavioral_sensor.py` (25–30% of E, 30–40% of I) and `src/confidence.py` (55%
of omitted-confidence fallback) belong to the prospective deconfounding shadow.
Full record: [`eisv-decision-records.md`](eisv-decision-records.md#decision-record--legacy-cv-is-control-feedback-not-health-evidence-2026-08-11).

## Decision record — retire legacy coherence authority outside the shadowed actuator (2026-08-12)

Non-actuator consumers (recovery eligibility/margin, stuck recovery, dialectic
triage, peer similarity/outlier ranking, state-announcement filtering, focus
stability, significance logging, anomaly/trend classification, knowledge-write
confidence, and auto-outcome quality) exclude legacy `C(V)` from authority. The
check-in actuator paths (configured coherence pause, CIRS, basin, adaptive
governor, ΔEta, and trajectory identity) remain deployed while their versioned
shadows collect prospective evidence. Full record:
[`eisv-decision-records.md`](eisv-decision-records.md#decision-record--retire-legacy-coherence-authority-outside-the-shadowed-actuator-2026-08-12).

## Decision record — cold-start confirmation remains shadow-only (2026-08-10)

Independent dialectic review (`8539c516649a08af`) rejected default-on two-step
confirmation actuation for fallback `risk_pause` events: confirmation remains
instrumentation-first (`shadow_would_defer` / `shadow_confirmed`), default-off,
and fail-closed until confirmation state is durably persisted and reviewed. Do
not conflate `policy_evaluation.maturity_gate.actuation_*` (scope
`fallback_risk_pause_deferral`) with `enforcement.*` (the live runtime circuit
breaker). Full record:
[`eisv-decision-records.md`](eisv-decision-records.md#decision-record--cold-start-confirmation-remains-shadow-only-2026-08-10).

## Decision record — non-authored cold-start authority guard (2026-08-10; amended 2026-08-22)

With `GOVERNANCE_NON_AUTHORED_COLD_START_GUARD=true` and
`GOVERNANCE_COLD_START_GUARD_INCLUDE_AUTHORED=true` (both default), an original
fallback-risk pause (`pause/risk_pause`, or `pause/cirs_block` with
`nearest_edge=risk`) whose complete decision-time hard-stop record proves
`risk_only`, verdict source `phi_cold_start`, primary EISV source
`ode_fallback`, and behavioral confidence `< 0.3` is downgraded to
`proceed/guide` (`enforcement.requested=false`) while preserving the full
original pause and trigger record in `epistemic_gate`. Structural/runtime-safety
stops (void, coherence floor, CIRS resonance, loop detection, low basin) and
missing/contradictory provenance still fail closed and pause. Full record and
policy-vocabulary table:
[`eisv-decision-records.md`](eisv-decision-records.md#decision-record--non-authored-cold-start-authority-guard-2026-08-10-amended-2026-08-22).

## Decision record — V-reintroduction deferred (2026-08-07)

Governed dialectic review (`3e003d82fb2d251e`) deferred reintroducing V as a
policy-layer leaky EMA of the outcome-channel residual: V stays out of the
certified 3-state core and off all governed/verdict surfaces. Any future V-EMA
build requires its own prior pre-registration (evidence minima, accrual and
settlement maths, and an incremental-information test against E, I, S and naive
baselines), a ≥30-day advisory soak, and CI guards proving V-absence from
verdict/pause/risk/coherence/gating paths. Full record and V consumer
inventory:
[`eisv-decision-records.md`](eisv-decision-records.md#decision-record--v-reintroduction-deferred-2026-08-07).

## Core mathematical posture

The public math should lead with proprioceptive residuals, not bad-outcome
classification. The live behavioral path is deliberately modest:

- **Warmup:** the behavioral track scores against fixed universal thresholds
  while the agent has too little history for individualized drift; the live
  verdict falls back to the mostly server-derived cold-start prior.
- **After warmup:** self-relative z-score deviation from the agent's own Welford
  baseline.
- **Always:** absolute safety floors and basin-health gates remain in force.

The roadmap target for richer cold-start grounding is a hierarchical reference:

```text
measurement_t = EISV_t
reference_t   = blend(agent_baseline_t, class_anchor; w(grounding))
residual_t    = measurement_t - reference_t
```

Until that hierarchical blend is the live path, public docs should describe class
or population anchors as roadmap semantics, not deployed authority. The stable
semantics are still residual-first: `residual_t` is information about state change
— running hot, brittle, scattered, or unusually careful — before it is a policy
concern. Deviation inside a healthy basin is room to learn, not proof of failure.

## Layer separation

The canonical stack is:

```text
measurement → diagnosis → policy → enforcement → external outcome evidence
```

| Layer | Owns | Must not pretend to own |
|---|---|---|
| Measurement | EISV, confidence, coherence, risk, phi, provenance | Outcome truth, blame, or punishment |
| Diagnosis | Interpretations such as strained, scattered, brittle, running hot | The authority to pause by itself |
| Policy | Rules mapping measured/diagnosed state to advice, review, or circuit-breaker candidates | Raw measurement truth |
| Enforcement | Actual pause/block/review/allow effects, with actor and mode | The reason an outcome was good or bad |
| External outcome evidence | Test/CI/tool/user/harm/verifier results, with provenance | Retrospective EISV self-justification |

A thermometer can inform a safety protocol; it does not itself pause the body.
Likewise, EISV can inform governance policy, but pause authority belongs to a
policy/enforcement layer.

## Evidence / label taxonomy

The word `bad` in storage and reports is a compact label class, not a moral
verdict. Human-facing docs should name the class whenever possible:

| Class | Examples | Validation use |
|---|---|---|
| `task-negative` | CI failed, test failed, command exit nonzero, answer was corrected | Useful for calibration and rework prediction; not automatically governance-bad |
| `contract/process violation` | Claimed tests passed without running them, hid uncertainty, ignored explicit constraints, fabricated output | Stronger governance signal because it breaks the work contract |
| `authority/harm` | Deleted data, leaked secrets, bypassed ask-first boundaries, charged money, spammed or impersonated a user | Strict governance-bad / escalation class when externally verified |
| `synthetic red-team fixture` | Known-safe adversarial cases, negative controls, containment probes | Validates plumbing and containment only; never production trust by itself |
| `unknown/unmeasured` | No verifier, no rubric, no external signal | Exclude from strict validation |

A normal mistake is usually task-negative feedback. It becomes governance-bad
only when it is tied to a contract violation, authority overreach, concealment,
or user harm.

## Sensory class split — which axis needs which validation regime (2026-08-17)

The contract header stays as written. `EISV is proprioception` does load-bearing
anti-verdict work and is asserted verbatim by
`tests/test_eisv_semantic_contract.py`, by the README, and by three generated
report headers. This section refines that claim rather than renaming it, because
one word is covering two senses whose validation regimes differ, and
`docs/ontology/paper-positioning.md` already records the honest framing as
"proprioception/**interoception**" with the interoceptive-inference prior art
(Seth 2013; Lee/Friston, arXiv 2309.05999; Tschantz/Seth/Pezzulo 2022).

The distinction is operational, not vocabulary:

- **Proprioception has an external referent.** There is a true joint angle, so
  acuity is scored as error against it. An axis in this class is validated by
  agreement with something outside itself.
- **Interoceptive constructs are not generally defined by one externally correct
  state value** and are often organised around homeostatic setpoints. Some
  interoceptive measurements still use physiological referents. The Garfinkel
  et al. (2015) measurement model, for example, splits into three dissociable
  dimensions:
  **accuracy** (performance against a physiological referent), **sensibility**
  (self-report), and **awareness** (confidence–accuracy correspondence). An axis
  in this class is validated by reliability, faithfulness and calibration, never
  by outcome agreement, because there is no outcome for it to agree with.

This document's prose has long used interoceptive analogies — "running hot",
"running careful", brittle, scattered, residual-from-own-baseline, and
"deviation inside a healthy basin is room to learn". A basin can be interpreted
operationally as a setpoint region; proprioception itself has no setpoints. Only
the label was proprioceptive.

### The split, as derived in `src/behavioral_sensor.py`

Anchors are function names, not line numbers: the line anchors this table first
carried had gone stale by 2026-09-25 and were replaced that day, re-read
against `src/behavioral_sensor.py` and `src/governance_monitor.py`. Weights are
as coded; the post-blends are applied in `compute_behavioral_sensor_components`.

| Axis | Derivation (weights as coded) | Class | Validation regime | Gated by the 2026-12-01 stop rule? |
|---|---|---|---|---|
| **E** | `_compute_E_components` — 35% decision success, 25% legacy `C(V)` level, 20% complexity calibration, 20% outcome success when at least 3 outcomes are recorded (`len(outcome_history) >= 3`; 40% / 30% / 30% otherwise); then a 20% continuity blend and 15% `1.0 - tool_error_rate` | **Externally referenced.** Decision outcomes, tool errors and outcome success are world facts, not self-state | Error against referent | **No** — blocked by a *join*, not by labels |
| **I** | `_compute_I_components` — 50% `1.0 - calibration_error`, 30% legacy coherence trend, 20% outcome consistency when at least 3 outcomes are recorded *and* at least 3 of them carry a finite `outcome_score` (60% / 40% otherwise, including 3+ outcomes with fewer than 3 finite scores); then a 20% continuity blend and 10% `unique_tools_ratio` | **Internally referenced / calibration-related.** Its dominant term is confidence-vs-correctness correspondence. *Research interpretation:* this may be compared with Garfinkel-style interoceptive awareness. | Metacognitive calibration (ECE / meta-d′) | **No** |
| **S** | `_compute_S_components` — 40% drift norm, 35% regime instability, 25% complexity divergence; then a 20% continuity blend plus a tool-velocity term | **Self-relative / deviation-dynamics.** How much is the runtime moving and switching; no single externally correct value exists. *Research interpretation:* this may be compared with homeostatic or arousal constructs. | Test–retest reliability + faithfulness under intervention | **No** |
| **V** (sensor) | `_compute_V` / `_compute_V_components(E_history, I_history)` — 60% (E slope − I slope) + 40% instantaneous E−I gap over the last 10 check-ins. This is the **sensor's** V. It reaches the ODE only through the sensor spring coupling (`k_anchor` in `governance_core/dynamics.py`, when `sensor_coupling_allows` admits the source), and otherwise lands only in measurement-only records (sensor–ODE divergence, derivation). It is **not the verdict V**: `GovernanceMonitor` passes only E/I/S to `BehavioralState.update`, whose V is an EMA of the raw imbalance `E_obs − I_obs` (`BehavioralState._raw_valence`, `V_FORMULA_VERSION = 2`) | **Not a sense.** Takes no exogenous input; a deterministic function of two other axes | None available — see below | n/a |

**E — the referent exists and is not joined.** E already ingests machine-checked
external signal: `tool_error_rate` at 15% and outcome success at 20%. That makes
E the one axis with a real external answer to be wrong about. The obstacle is
neither epistemic nor the stop rule: `docs/EVALUATION_INDEX.md` records that the
tool-failure population and the EISV-bearing population are **disjoint**. The
referent exists; the two are not joined. That is plumbing. *Update
2026-10-12 (ledger row 47):* the tool term is joined on 99% of check-ins but is
almost always zero, externally verified outcomes reach a median 0% of E's
outcome history, and a median 75% of the within-identity variation in E's sensor
observation comes from the agent's own prior verdicts.

**I — the calibration referent is currently circular.** I's dominant term is
`1.0 - calibration_error`, which is precisely Garfinkel's third dimension. The
tool that measures it, `scripts/analysis/report_calibration.py`, is flagged
possibly-stale in the evaluation catalog and carries a second defect: its line 29
states the target as "Do agents with high confidence end up in **healthy
states**?" — it calibrates reported confidence against EISV's own health readout.
The **Validation rule** below already forbids exactly this: never validate EISV
by letting EISV create its own labels. The repair is to repoint the referent at
the external signals E already ingests, not to rebuild the script.

**S — validatable today, entirely offline.** Drift norm, regime-transition
counting and complexity divergence are self-relative dynamics with no single
externally correct value. That makes a homeostatic interpretation plausible as
research context, rather than establishing S itself as a homeostatic construct.
That removes outcome agreement from its regime and leaves the two checks the
interpretability and observability literatures actually use: test–retest
reliability (same state, same reading) and faithfulness under intervention
(inject a known regime change; does S respond with the predicted sign and
magnitude). Both are synthetic. Neither needs labels, independent operators, or
the stop rule.

**V — condition 2(c) is already answered for the behavioral channel.** The
V-reintroduction deferral (2026-08-07, dialectic `3e003d82fb2d251e`) binds any
future V work to condition 2(c): an incremental-information test against E, I and
S that V must pass to count as an independent axis. For **behavioral** V that
direct incremental-information path is **PATH BOUND** — `_compute_V` reads only
`E_history` and `I_history`, so it cannot add exogenous information beyond those
inputs. This is a source/path result, not a negative experiment and not a finding
about the policy-layer outcome residual. This does not
disturb the deferral: the deferred object is the *policy-layer EMA over the
outcome-channel residual*, which does take exogenous input, and roadmap §4d
already separates the two channels. This record only fixes which channel the
condition is answered for, so no future session re-runs an unwinnable test on the
wrong V.

### Dead travel in E and I

E draws 25% of its weight from the legacy `C(V)` level and I draws 30% from its
trend. The 2026-08-11 record above establishes that scalar is pinned ≈0.49 by
near-zero ODE V. A pinned input is not noise, it is dead travel: roughly a
quarter of E's and a third of I's expressible range is currently a constant,
capping the sensitivity those axes can show before any question of outcome lift
arises. This is measurable by arithmetic and needs no labels.

Recorded, not repaired. The 2026-08-11 record already rules that removing the
coupling moves behavioral baselines and verdict inputs, and belongs in the
prospective deconfounding shadow.

### What this changes

Nothing deployed. No gate, threshold, weight, verdict path or stored field
changes here; this section classifies what would *count* as validating each axis.

The operational consequence is scheduling. The 2026-12-01 outcome-grounding stop
rule (#1425) governs **outcome-label discrimination** — whether EISV separates
good outcomes from bad ones. Three of the four regimes above are not
outcome-label questions. S's reliability and faithfulness, I's calibration
referent repair, and E's referent join are unblocked by that stop rule and need
neither independent operators nor external labels, so they should not sit queued
behind it. The instrument-frame tooling that has been allowed to rot
(`report_calibration.py` possibly-stale, `eisv_pca_analysis.py` won't-run) is
precisely the tooling those three regimes require.

## Validation rule

Never validate EISV by letting EISV create its own labels. A prospective claim
needs all of the following to be meaningful:

1. a prediction or prior-state snapshot recorded before the outcome;
2. an external label source or pre-registered rubric;
3. enough negative-class coverage for the lane being scored;
4. comparison against boring baselines such as previous outcome, task type,
   confidence, recency, and harness lane;
5. language that distinguishes internal signal, predictive lift, policy effect,
   and enforcement effect.

Red-team data is useful, but only if labeled as red-team data. It answers "can
we detect this frozen failure mode?" It does not answer "does EISV generally
judge agents correctly in production?"

### Comparing rows across instrument changes (2026-10-16)

The EISV maths changes often, and the `epoch` column cannot mark it: every
runtime read of state, history, baselines and calibration filters on
`epoch = CURRENT_EPOCH`, so a bump resets every agent. It has been 3 since
2026-04-27 and none of Stage A, #2831 or #2834 moved it. Instead:

- `config/eisv_instrument_eras.json` lists each known change with its best
  effective time, how that time is known (deploy restart, deploy reflog, epoch
  row, first row carrying a new key, merge), and the interval in which it may
  have gone live. Rows inside that interval belong to neither side. Times before
  2026-08-27 are mostly merge or first-seen times; several flag changes left no
  time at all and carry an approximate one.
- Every row written from 2026-10-16 carries `state_json.eisv_instrument`: the
  era, a hash of the effective configuration (dynamics parameters, flags,
  class-calibration tables, resident roster, decision thresholds), the build
  sha, and `updates_since_change`, the monitor's check-ins since its instrument
  last changed, with `carried_from` naming the previous instrument. A monitor
  restored across a change carries ODE state integrated under the old maths and
  relaxed for 10–15 minutes after #2834, so those rows are a transient.
- `src/eisv_eras.py` holds the read side: `require_single_era` refuses a window
  that crosses a change unless the caller gives a reason, `era_windows` splits a
  window for per-era reporting, `require_single_instrument` refuses stamped rows
  that carry more than one instrument id (a threshold, flag or overlay change
  inside one era), and `is_settled` drops the restore transient.
  `check_window` wraps both checks for a script and returns the provenance
  lines to print beside its result.
- Wired readers (each refuses a crossing window unless given
  `--allow-cross-era REASON`, and prints the crossed changes and stamped ids):
  `basin_conjunct_binding_read.py`, `eisv_history_structure_read.py`,
  `coherence_gate_shadow_read.py`, `outcome_validation.py`,
  `eisv_phi_telemetry_redteam.py`, `eisv_residual_autocorr.py` and
  `eisv_stage_b_safety_floor.py` under `scripts/analysis/`, plus
  `scripts/verdict_counterfactual.py` and `scripts/dev/grounding_flip_analysis.py`.
  The registered-read path (`eisv_skeptic_report.fetch_rows`, the ablation
  matrix, the input snapshot, the prospective cohort) is deliberately not wired:
  it has its own boundary at T and changing it would change the 12-01 read.
  Readers that run on a schedule or serve live traffic annotate instead of
  refusing (`crossing_report`, `crossing_note`): the doctor's
  `signal_degeneracy` check names the crossed changes in its detail, and
  `/v1` telemetry-health returns them under `eisv_instrument_eras`. Both are
  time-based only. Not yet wired: the class-anchor regeneration and all-time
  trajectory reads.
  `scripts/analysis/eisv_local_frame.py` (2026-11-02) reads the current era
  only and frames each agent against its own frozen reference within one sensor
  instrument and one instrument id. It is advisory: it compares no agents with
  each other and sets no verdict, action or stored value.

There is no per-field exemption. A change that only touches the basin or the
action still moves behavioral E, I and V on later check-ins, because the action
feeds behavioral E through `decision_e` and the behavioral observations are
partly built from ODE state (row 13). A change appends a registry entry; a
change to the golden trajectories without one fails
`tests/test_eisv_instrument_eras.py`. Report each era separately with n,
identity count and p50. A recomputation of older rows under current maths is a
labelled estimate, not extra sample.

### Reference frame, chart and metric (2026-11-02)

Three things are easy to call "coordinates", and UNITARES has them to different
degrees.

- **Reference frame: the record.** Identity with declared lineage, claims and
  their supersession links, reviews, outcomes and time locate every piece of
  work. They give addresses and order, not distances. Declared lineage is an
  attributed assertion, and full supersession traversal needs the AGE backend.
- **Chart: EISV's shared axes.** The v6 paper places every agent on one state
  space, $\mathcal{X} = [0,1]^3 \times [-1,1]$, and treats class as an affine
  rescaling of that shared coordinate system. The chart is declared, not
  validated.
- **Metric: comparable distances.** None is validated. A difference between two
  EISV points is comparable only within one instrument (`sensor_eisv_source`) and
  one instrument id (era and configuration, see the section above), and even
  there it is descriptive, not outcome evidence. The two sensor classes sit at
  different levels on the same axes (§Sensory class split), and the registry
  lists many changes of scale.

What an agent is compared against is a fourth thing. During warmup it is the
universal thresholds; after warmup, the expanding per-agent baselines. Those
baselines filter on `epoch`, not on instrument, so today "its own normal" spans
every instrument change since 2026-04-27.

A shared metric for one sensor class would need, fixed before the data are read:
the maths unchanged for at least eight weeks; agreement between per-agent levels
in adjacent non-overlapping windows; instrument and era not recoverable from the
shared values; a predictive gain on held-out agents over a baseline of agent
age, task type, model and instrument; and a gain over per-agent frames on a
named task. A new label channel makes such a test possible; it is not a result.
A PASS of the registered 12-01 read does not supply this, because between-agent
level differences alone can clear its first two conditions.

### Named validation and falsification paths (2026-10-16)

The canonical doctrine above makes comparative decision value against simpler
representations the burden of proof. Two proposed experiments, both written up
on 2026-09-22 alongside the doctrine (#2342 step 5), are the named external
tests of that burden. Neither has run, and neither is evidence for or against
EISV. Both treat a null result as a valid outcome.

| Path | What it would test | What would count against EISV | Status |
|---|---|---|---|
| Resource-grounded E ([#2410](https://github.com/cirwel/unitares/issues/2410)) | Whether resource telemetry seen at the harness (context headroom, tool failures, compaction) makes runtime decisions better than current E, **and** better than raw resource thresholds that need no EISV | Raw thresholds do as well. E should then not absorb them, and using thresholds directly in policy is a legitimate result | **Parked 2026-09-24** after adversarial review (see the [disposition comment](https://github.com/cirwel/unitares/issues/2410#issuecomment-5812730348)). Its compaction outcome is decided by the resource signal itself, its tool-failure outcome would amount to an EISV discrimination read inside the pre-read window, and no decision consumes the result. It may be revisited after the 12-01 read reports, and only as a resource-signal arm inside the incremental-value ablation (`docs/proposals/eisv-incremental-value-ablation-v1.md`) |
| Trajectory-quality shadow evaluation on an external agentic-RL harness (Xiaomi MiMo-V2.6 / Uni-Agent) | Among sibling rollouts of the same task that **all pass** the terminal verifier, whether prefix UNITARES observations predict a later, independently assigned quality or reward-hacking label better than task, harness, length and tool-count baselines. Telemetry is observation-only and causal; nothing reaches generation, reward, routing or stopping | No out-of-sample information beyond those baselines (H0), or no transfer across at least two harness strata | **Proposed, not filed, not started.** It exists as a protocol (v0.1), an adapter boundary and an issue draft in UNITARES knowledge-graph notes of 2026-09-22. It needs access to sibling rollouts with independent groupwise quality labels, which this deployment does not have. An outcome read from it in the pre-read window would raise the same objection that parked #2410, so it waits for the 12-01 report. Its independent label channel would make it a candidate new premise under the stop rule's reopening clause |

Phrase them as candidate falsification and validation paths, not as
supporting work. "MiMo" names the external system whose evaluation path would
supply the labels; it does not imply that system validates EISV or UNITARES.
Either path that later produces a result enters the ledger below like any
other claim, at its measured level.

## Tested claims — ledger

Added 2026-07-29. This section exists because the doctrine above was already
correct in June and nine surfaces drifted anyway. Doctrine states what we may
say; this ledger states what we have actually tested, with the mechanism cited
to line. Statuses are deliberately distinct:

| Status | Meaning |
|---|---|
| `EARNED` | a pre-registered test of the stated estimand passed its predeclared standard |
| `REFUTED` | a pre-registered, adequately powered test of the stated estimand failed its predeclared standard; no current EISV outcome claim has this status |
| `BENCHMARK FAIL` | an exact registered estimator or mechanism missed its benchmark; transfer to a deployed capability requires separate identification |
| `IMPLEMENTATION MISMATCH` | direct source, configuration, or algebra contradicts an exact description of the deployed mechanism; this is an engineering finding, not a negative experiment |
| `FORMULA MISMATCH` | deployed algebra differs from the named interpretation; the formula is characterized, not scientifically falsified |
| `PROVENANCE MISMATCH` | a label or signal source differs from its stated provenance; downstream outcome meaning must be re-identified |
| `DOCUMENTATION MISMATCH` | documentation and deployed behavior disagree; this is a correction obligation, not experimental evidence |
| `PATH BOUND` | source or algebra excludes a named direct path under stated configuration; recursive, counterfactual, and broader capability claims remain open |
| `EVENT RECONCILED` | audit records distinguish what was computed, recorded, delivered, or suppressed for a named event |
| `DEPLOYMENT SNAPSHOT` | a dated census describes this deployment; rows and events are not independent trials and the result does not generalize by itself |
| `INCONCLUSIVE` | a usable test ran but its interval, support, or decision standard did not resolve the claim |
| `UNIDENTIFIED` | the design cannot estimate the target claim because the counterfactual, independence, or causal/predictive identification is absent |
| `WITHDRAWN` | an earlier inference was invalidated by a scope, provenance, dependence, or instrument defect |
| `UNTESTED AS DEPLOYED` | a test ran and returned a negative, but the instrument had no usable power against the claim |
| `LABEL-BLOCKED` | not testable at current external-label supply |

`REFUTED BY CONSTRUCTION` is retired as an active status. It collapsed source
review, path analysis, and scientific falsification into one negative-sounding
bucket. Historical uses map to `IMPLEMENTATION MISMATCH` or `PATH BOUND` and
remain searchable as provenance; neither status is evidence against UNITARES as
a capability or against an outcome-level hypothesis.

The initial 2026-08-22 correction and its five reclassified claims are recorded
in
[`falsification-inference-containment-2026-08-22.md`](falsification-inference-containment-2026-08-22.md).
The systemic follow-up audits all 45 ledger rows, the outcome-test design, and
the recurring read protocol in
[`falsification-design-system-audit-2026-08-23.md`](falsification-design-system-audit-2026-08-23.md).

Full 54-row source-cited audit ledger, wave notes, and dated SQL snapshots:
**[`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md)**.

### Summary of core audited claims (rows 1–5)

- **1. Individuality axiom** → **UNTESTED AS DEPLOYED.** Pre-registered v2
  (`e512c01c…`) returned FAIL on 2026-07-30 and the kill criterion is honoured
  (retired for raw behavioral EISV as currently measured; no v3 without changing
  the measurement process). Not evidence against the axiom: Leg A's block-permutation
  drift veto is a whiteness detector that false-fires on stationary correlated
  processes, and Leg B ran at effective n=3 behavioral agents (`p_min = 1/3! = 0.167 > α`).
- **2. Individuality of the I dimension** → **IMPLEMENTATION MISMATCH in
  calibration scoping; individuality remains UNIDENTIFIED.** `cal_I` (50–60% of
  I) is a fleet-shared scalar on each tick, diluting I's contrast, though 40–50%
  of I remains agent-local and a shared additive term cannot move a Spearman
  rank statistic.
- **3. The per-agent reference does useful work** → **BENCHMARK FAIL for the
  cold-start reconstruction; UNIDENTIFIED for the warmed deployed EMA.** Leg C
  cold-started the mirror at the registration boundary (0/7 beat persistence on
  2026-07-29), so the warmed deployed reference remains unidentified.
- **4. Outcome validity** → **WITHDRAWN FOR TARGET INFERENCE; unresolved pending
  the registered trusted-anchor read.** Historical `--anchor-scope all` runs are
  withdrawn provenance. The registered read (`#1425`) ran on 2026-11-03 and
  closed `INCONCLUSIVE` for insufficient eligible evidence after its fixture
  rule removed 4,649 of 4,876 trusted outcomes (`bad_clusters` 29 and 28 against
  150); the non-authoritative corrected-rule sensitivity run (449 bad clusters)
  found no detectable rework-prediction lift over the previous-outcome baseline
  (selective p 0.858 and 0.828). Reopening requires a new premise (a different
  label channel or measurement process).
- **5. Bounded and mean-reverting** → **partly TRUE BY CONSTRUCTION** (hard
  clips in `src/behavioral_state.py` and floor in `src/behavioral_sensor.py`).

### Trajectory identity — same discipline, different instrument

Rows **6–12** in [`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#trajectory-identity--same-discipline-different-instrument)
audit `TrajectorySignature.similarity()` and `lineage_similarity`:

- **Rows 6–9 (`IMPLEMENTATION MISMATCH` / `PATH BOUND`):** Six weighted
  components reduce to roughly two quantities (η and β alias α and ρ; Π's
  `preferences["vector"]` is never emitted by the producer; ρ is 0.20 of free
  credit because coherence never dips below 0.46; α is double-eps regularized
  on ODE histories, collapsing mature identities toward ~0.633).
- **Rows 10–12 (`IMPLEMENTATION MISMATCH` / `DEPLOYMENT SNAPSHOT`):**
  `lineage_similarity` is live-affecting (`@enrichment(order=170)` shifts
  agent-visible `risk_score` by +0.15/+0.05/−0.05 and feeds identity-assurance
  upgrades), drift alerting has not discriminated across the fleet, and Pi-side
  (`anima-mcp`) genesis lacks numpy covariance and fails by an inverse mechanism.
- **Do not:** lower the 0.6 anomaly cut · widen eps or the covariance floor ·
  promote the Pi's genesis `variance` list to a diagonal covariance matrix ·
  re-freeze or delete a genesis or run the rebaseline script outside an
  operator-attested cutover · revive Π without checking for α aliasing · build a
  reject or pause on `lineage_similarity` · cite trajectory identity as
  validated or discriminating.

### Core governance math — 2026-08-06 wave

Rows **13–23** in [`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#core-governance-math--2026-08-06-wave)
cover the sensor→verdict loop, coherence gates, calibration consumers, and
stability telemetry:

- **Rows 13–23:** Behavioral EISV ingests ODE state (`C(V)` level/trend and
  regime instability) and spring-couples back into the ODE (row 13); post-warmup
  content-movable risk components sum to 0.45 below the 0.60 cut (row 14);
  fleet-shared calibration feeds `noise_S` and `ΔEta` (row 15); deployed legacy
  `C(V)` spans ~[0.47, 0.51] and cannot cross `tau_floor=0.25` (row 16);
  `metrics['stability']` evaluates static default params at zero drift (row 17);
  `AdaptiveGovernor` thresholds saturate fleet-identically (row 18); the 0.05
  sigma floor dominates I z-scores for 96% of baselined agents (row 19); E is
  majority-degenerate from `decision_e` while S varies (row 20); `ΔEta`
  Consistency is fixed at 0.8 (row 21); `auto_ground_truth` and unfiltered
  sensor outcomes are largely self-referential (row 22); resident class
  calibration requires `UNITARES_CLASS_CALIBRATION` (row 23).
- **Do not:** cite `governance_monitor.py`'s internal sensor fallback as the
  live handler wiring · silently "fix" the pause-scores-0.5 mapping without a
  loop decision · loosen the Phase-5 auto-emit calibration gate (0.65) · treat
  `metrics['stability']` or `drift_forecast` as deployed setpoint dynamics ·
  read the roadmap §6 status table as current without checking live config.

### Bootstrapping / phi path — 2026-08-06 wave 2

Rows **24–44** in [`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#bootstrapping--phi-path--2026-08-06-wave-2)
audit the cold-start/Phi verdict path and enforcement delivery:

- **Rows 24–44:** `gap_suppress` arms at any inter-check-in gap >150s and
  downgraded 89.4% of recorded pauses in the 90d snapshot, though first
  check-ins can actuate the circuit breaker (rows 24, 27, 28, 40, 41); updates
  1–2 are Phi-owned while `bootstrapping` spans updates 1–9 and authority flips
  at update 3 (rows 25, 26, 34, 43); `adversarial_rho` is a magnitude-blind ODE
  sign product (`dE·dI`, bimodal 0 or 0.15) that drives behavioral cautions and
  resonance flips (rows 31–33, 38, 42); S-setpoint invariance holds at rest
  after the 2026-10-15 basin/ODE repairs (row 36).
- **Do not:** alter `gap_suppress` or the 150s arm as a tuning tweak · read a
  produced pause verdict count as delivered enforcement · enable
  `VERIFICATION_FLOOR_ENABLED` as a quiet patch · infer verdict authorship from
  the `bootstrapping` phase label · treat `cirs_resonance` or `adversarial_rho`
  as graded severity evidence.

### Decision self-loop at the basin — 2026-08-21

Rows **45–49** in [`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#decision-self-loop-at-the-basin--2026-08-21)
audit endogenous feedback loops and E/I/S referents:

- **45.** "The guide self-loop can flip an agent across the `E ≥ 0.6` basin
  bound and hold it there." — **PATH BOUND for the direct same-check-in
  path; the recursive dynamic counterfactual is UNIDENTIFIED.** `classify_basin`
  reads ODE `GovernanceState` rather than behavioral E directly, and the earlier
  `"adequate power"` and `"cannot flip and hold"` interpretations are withdrawn.
  **Sequencing rule:** any change feeding behavioral/primary EISV directly to
  `classify_basin` must neutralize or re-score `decision_e` in the same change
  (or ship behind a preregistered recursive shadow).
- **Rows 46–49:** S's `_regime_instability` term largely measures S's own
  step-to-step variation at rest (row 46); within an identity a median 75% of
  variation in E's sensor observation comes from `decision_history` (row 47);
  I's fleet calibration scalar was constant at 0.1474 across the 30d window
  (row 48); partially pooled per-agent I calibration remains exploratory pending
  target-bound verified forecast/outcome pairs (row 49).

### Decision path and dynamics — 2026-10-15

Rows **50–54** in [`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#decision-path-and-dynamics--2026-10-15)
record the October 15 audit and repairs (`#2831`, `#2834`):

- **Rows 50–52 (`Repaired 2026-10-15`):** `classify_basin`, ODE E/I derivatives,
  fresh-monitor S seed, and regime fallback now detrend ODE S by `s_setpoint`
  (σ) via a single resolver (`setpoint_for_monitor`), and the Φ-derived
  `latest_risk_fast_trip` is disabled when `behavioral_assessment` owns the
  verdict.
- **Rows 53–54 (`IMPLEMENTATION MISMATCH`):** `AdaptiveGovernor` overrides
  `parameters.delta=0.4` with `state.delta` (starting at 0.25, PID in
  `[0.15, 0.50]`), and the handler path computes behavioral sensor drift and
  continuity from check-in `t−1` (`_last_drift_vector` /
  `_last_continuity_metrics`).

### Fitted and learned estimators — which bucket they sit in (2026-09-25)

Outcome-fitted or outcome-learned estimators sit in the **efficacy** bucket and
are governed by the registered stop rule (`#1425`): a pure refit on the same
trusted task/test label channel is not a "new premise" under the reopening
clause. Label-free instrument-frame work (self-predictability, windowed
baselines, system identification) may proceed offline, while any live-path
change must record an era boundary in `config/eisv_instrument_eras.json`. Full
placement note and ODE-fallback reach snapshot:
[`eisv-tested-claims-ledger.md`](eisv-tested-claims-ledger.md#fitted-and-learned-estimators--which-bucket-they-sit-in-2026-09-25).

## Preferred wording

Use:

- "EISV/prior-state telemetry"
- "proprioceptive signal"
- "agent strain / incoherence / overload"
- "policy input"
- "task-negative / contract / authority-harm outcome label"
- "external outcome evidence"

Avoid:

- "EISV decided this was bad"
- "EISV prevented harm" unless an enforcement path actually did
- "UNITARES paused / halted / enforced / blocked the agent" for a *produced*
  verdict — policy is often advisory but the circuit breaker is live (see
  "Deployed posture"); say "produced a pause verdict" or "would-pause" unless
  `enforcement.applied=true` or a matching `lifecycle_paused` event exists
- a pause / high-risk verdict *count* presented as an enforcement or
  harm-prevention count
- "bad outcome" without naming the label source/class
- "validated EISV" from synthetic fixtures, single-class strict scope, or
  retrospective self-labels
- "we judge each agent against its own normal" as a *validated* property — the
  mechanism exists, the validation does not (ledger rows 1–3)
- "the individuality axiom was refuted / disproved / tested and failed" — the
  pre-registered test failed, which is not the same claim (ledger row 1). Say
  "untested as deployed" and cite the instrument's power
- "rolling" for the Welford per-agent baselines — they are expanding, and the
  difference changes how a fixed z-threshold behaves over an agent's lifetime
- "coordinate system" for the record — say "reference frame"; it has addresses
  and order, not distances
- EISV distances, rankings or "closer to healthy" comparisons across sensor
  classes or instrument eras — the chart is shared, the metric is not validated
  (§Reference frame, chart and metric)
- trajectory similarity (`lineage_similarity`) as evidence of identity — identity
  is declared in the record; the score tracks identity age (ledger, trajectory
  identity)

## What ablations should say

Ablations should ask whether EISV/prior-state telemetry adds predictive signal or
useful policy steering over baselines. They should not imply EISV is the judge of
badness or the component handing down bad verdicts. The skeptical report,
inventory, and prospective cohort reports should make weak data obvious: sparse
bad labels, synthetic fixtures, unbound prediction IDs, missing prior-state
coverage, and harness-lane contamination are data-quality limits, not
philosophical failures of proprioception.

## Prior art / positioning

EISV's proprioceptive framing is **informed by and structurally adjacent to work
on interoceptive inference; it is not presented here as a formal implementation
of that theory** (prior-art audit:
`docs/ontology/trajectory-identity-prior-art-2026-06.md`). The
"sense your own internal state, keep it within viable bounds, before any verdict"
posture has theoretical neighbors in work on interoceptive inference: Seth (2013), *Trends in Cognitive Sciences*
17(11):565-573; the Friston-co-authored "Life-inspired Interoceptive AI" (arXiv
2309.05999), with its self/world Markov-blanket factorization; Tschantz, Seth &
Pezzulo (2022), *Biological Psychology* (interoceptive control as prediction-error
minimization against homeostatic/allostatic set-points); and the Interoceptive
Machine Framework (2026), *Physics of Life Reviews*.

Two cautions follow, both consistent with the rest of this contract:

- **Neighbor, not grounding.** Cite these as theoretical neighbors and research
  context for EISV, not as grounding or evidence that EISV formally instantiates
  their constructs. Do **not** claim EISV's coordinates are variational
  free-energy quantities —
  that grounding claim retired with the v7 F-hat spike (see
  `paper-positioning.md`, 2026-04-23). The "thermometer, not a court" framing
  here is *the same* pre-judgmental stance the interoceptive literature gives
  interoception: it informs regulation, it does not adjudicate.
- **Novelty window.** The interoceptive-AI literature is converging quickly
  (2024–2026). Positioning EISV in relation to established work on interoceptive
  inference and adjacent control frameworks, rather than presenting those ideas
  as novel theoretical discoveries, is the honest and durable framing.
