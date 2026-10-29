# 5 · Reading the record

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)

This chapter reads the record in the order a successor or operator should:
findings first, then outcomes, then the proprioceptive recommendation and the
separate actuation boundary. The last section covers EISV, the behavioral state
estimate behind that recommendation: what its numbers mean, how they are produced, and how much to
trust them. EISV still ships in the core path; its move to an optional module
follows a [staged plan](../proposals/active/eisv-core-boundary-v0.md). The exact
formulas with source references are in
[`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md).

## 5.1 Findings and shared memory

Findings are the durable part of the record. Searching them *before* acting is how agents avoid re-discovering known failures, and it's where closed mysteries and corrected conclusions live. Discipline (agent-facing): **search before writing**; prefer a linked correction or `supersede` (which needs the AGE backend) over a fresh note; store something only when a future agent would search for it and not already find it. Operational runbooks belong in `docs/`, not KG notes. Full operating manual: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).

## 5.2 Outcomes and their trust boundary

The system records stated `confidence` and result evidence — test pass/fail,
command exit codes, lint results, or review labels — through
`record_result` / `outcome_event`. These records support calibration analysis.
Pass the `prediction_id` returned by `sync_state` to `record_result`: it binds
the outcome to that check-in's stated confidence. Without it, an explicit
`confidence` argument is used as given; otherwise the server falls back to the
most recent stated confidence, which is marked scraped and excluded from
calibration.

Evidence is not automatically independent merely because it is called an
outcome. CI-, tool-, or operator-authored records provide a stronger anchor than
an outcome authored by the monitored process. If the agent controls both its
confidence and every result record, it can forge a calibrated story. Preserve
provenance and keep high-stakes outcome channels outside the agent's authority.

## 5.3 The policy action

Each `sync_state` check-in returns an effective action and preserves the raw
policy recommendation. Internally the EISV assessment emits a `safe` /
`caution` / `high-risk` interpretation. `policy_evaluation.action` records what
the policy engine recommended; `action_summary.action` records what the agent
is operationally being asked to do; `enforcement` records whether authority was
actually applied.

The deployment posture is operator-owned:

- `UNITARES_POLICY_ACTUATION=observe` (default) keeps EISV/risk/CIRS
  observational. A raw pause recommendation becomes `proceed` / `guide`, with
  `enforcement.applied=false`; it creates no pause token and blocks no write.
- `UNITARES_POLICY_ACTUATION=enforce` retains the historical circuit breaker.
  A raw pause becomes an applied lifecycle hold.

| Effective action / posture | Internal sense | Agent action | Recovery path |
|---|---|---|---|
| `proceed` | safe | Continue | — |
| `proceed` / `guide` | slightly off | Read guidance, adjust | — |
| `proceed` / `guide`, `enforcement.posture=observe`, recommended `pause` | instrument crossed an inferred policy edge | Inspect the recommendation if useful; continuing is neither permission nor a safety certification | none required; dialectic remains optional |
| `pause`, `enforcement.posture=enforce`, `applied=true` | compatibility hard stop: check-ins and new shared-memory entries refused | Stop, read the reason | usually dialectic, operator, or expiry; self-recovery only if risk is already under its gate ([§5.4](#54-recovery-self-recovery--dialectic)) |
| applied `pause` / `reject` | legacy label for significant concern | Human input or optional dialectic | dialectic or operator |

How the risk behind a pause is computed, and the margin that rides along with
the action, are in [§5.5](#risk-components-and-margin).

## 5.4 Recovery: self-recovery → dialectic

When an agent has an **applied** pause, recovery is a structured escalation.
An observational pause recommendation does not enter this workflow:

1. **Self-recovery** — `self_recovery(action="check")` reports which path the current state allows. A paused agent cannot write the check-in that would lower its risk, so self-recovery succeeds only when the reading that paused it is already under the gate below; most pauses end through the dialectic, an operator, or re-evaluation at expiry. `self_recovery(action="quick")` resumes without reflection when risk is low and no void is active; when risk is moderate, `self_recovery(action="review", reflection=...)` resumes after the agent states what went wrong and what it will change. Legacy `C(V)` remains visible as ODE-control diagnostic context, but it does not authorize or deny recovery.
2. **LLM-assisted dialectic** — when requested, a configured reviewer model supplies an
   antithesis for single-agent reflection; the default reference path supports a
   local model.
3. **Peer dialectic** — optionally request one with `request_review` (that is,
   `dialectic(action="request")`); a peer reviews through thesis → antithesis →
   synthesis.

Full protocol: [`../dev/CIRCUIT_BREAKER_DIALECTIC.md`](../dev/CIRCUIT_BREAKER_DIALECTIC.md).

## 5.5 The EISV instrument

EISV is one instrument attached to check-ins, not the record itself. Read it
as a proprioceptive hypothesis about current state; read the findings and outcomes above
for what the work was and what happened.

### The pipeline at a glance

```
observables ──► observation blend ──► EMA state ──► residual / basin risk
(decisions,     (behavioral_         (behavioral_    (behavioral_
 calibration,    sensor.py)           state.py)       assessment.py)
 drift, tools)                                             │
                                      recommendation ──► operator posture
                                      proceed/pause       observe/actuate
```

The **behavioral EISV** path blends runtime observations and controller history, then smooths them into four indices. It owns the assessment after cold start. The **ODE controller** (`governance_core/`) also affects the final decision: its state supplies structural basin, void and coherence checks, and its coherence and history feed later behavioral observations. The two paths are coupled.

The current implementation has three stages: check-ins 1–2 use the Φ
cold-start prior; check-ins 3–24 use the behavioral assessment with fixed
universal thresholds; and check-in 25 enables per-agent **Welford-baseline**
z-scores against a 30-update target. Read the state as telemetry against the
reference available in that stage, not as an outcome judgment. During cold
start the response marks `action_summary.verdict_confidence: "provisional"`,
and a cold-start risk estimate does not hard-pause. The exact gates are in
[EISV_COMPUTATION.md](../EISV_COMPUTATION.md).

### The four dimensions

| Coord | What it records | Deployed computation (heuristic) |
|---|---|---|
| **E** · Energy | Weighted observations and prior judgments | recent decision scores + legacy controller coherence level + complexity divergence; available outcome, continuity and tool features also contribute |
| **I** · Integrity | Calibration and consistency features | calibration error + legacy controller coherence trend; available outcome-score consistency, continuity and tool-variety features also contribute |
| **S** · Entropy | Deviations and activity features | drift-norm + regime transitions + complexity divergence; available continuity and tool features also contribute |
| **V** · Valence | Direction and persistence of the E/I observation gap | separate EMA of raw `E_obs − I_obs` |

Three things to internalize:

1. **Risk scoring becomes self-relative after warmup; the coordinates remain weighted indices.** Assessment compares them with the agent's own baseline as well as absolute thresholds. A low raw E or I is not itself a measurement of low capacity or false claims.
2. **V carries a distinct temporal summary.** Its smoothing rate differs from E's and I's, so it is not generally their current smoothed values subtracted. Positive V records a positive smoothed `E_obs − I_obs` gap; negative V records the reverse. “Running hot” and “running careful” are interpretations to investigate, not facts established by the sign.
3. **Controller feedback is part of the record.** Prior decisions enter E, so a policy response can change later E even with unchanged task and tool features. That reflects the controller's response without independently confirming its judgment. The blend weights are hand-set engineering parameters; the exact formulas and defaults are in [EISV_COMPUTATION.md](../EISV_COMPUTATION.md).

### Risk components and margin

Behavioral assessment risk is the **sum of named components** (`low_E`, `low_I`, `high_S`, `high_V`, `adversarial_rho`, `high_CE`), each with an explicit weight — with each contribution available for inspection ([`src/behavioral_assessment.py`](../../src/behavioral_assessment.py)). **Absolute safety floors always apply to their named component**, overriding the self-relative component via `max`; they are not a verdict floor by default, so one breached dimension can still leave the behavioral verdict `safe`. The full result reports this separately under `behavioral.assessment.absolute_floor_observation`; it is telemetry, not a policy input. (An operator can enable a verdict floor for baselined agents with `UNITARES_FLOOR_BREACH_CAUTION_APPLY`; rows whose behavioral verdict it raised are labelled `policy_effect: behavioral_verdict_raised`. The label says only that the floor raised the behavioral verdict; the audit row's decision reason and final verdict show whether the decision changed.) Self-relative deviation risk is also *gated by absolute basin health* — inside the healthy basin, deviation from your own norm is treated as information, not danger.

**Margin** (`settling` / `comfortable` / `tight` / `warning` / `critical`) rides along with the verdict, reporting the distance to the nearest *decision threshold* (risk, void or coherence), not to a basin boundary, even while the verdict is still `proceed`. Read `margin_scope` and `unmeasurable_edges` with it: under the legacy coherence producer the coherence edge is not assessed, so `comfortable` covers only the measured edges. Threshold distance is a different notion of edge from basin membership: an agent inside the `boundary` basin can still report `comfortable`. The levels come from `GovernanceConfig.compute_proprioceptive_margin`.

### Calibration input to I

The calibration input currently blended into I on the handler path is a
lifetime **fleet** aggregate of strategic confidence-versus-trajectory-health
bins. It is not an accuracy reading for the current claim or just this agent;
the agent-scoped candidate is measurement-only. I also includes controller
history and optional consistency features. Inspect the producer and scope in
[EISV_COMPUTATION.md](../EISV_COMPUTATION.md#step-1--observations-srcbehavioral_sensorpy)
before interpreting a change as improved or worsened calibration.

### Peer comparisons

Like recovery, peer comparisons do not lean on legacy coherence.
Peer-similarity scores use E/I/S/V,
controller state, maturity, regime, and verdict where available; legacy
`coherence` is reported with producer/role tags but excluded from ranking. The
old `min_coherence` state-announcement filter is rejected, and the former
coherence-variance `focus_stability` value is explicitly retired rather than
recalibrated around a nearly constant producer.

### Reading the instrument and evaluating its uses

EISV records specified runtime features and controller history using auditable
blends and smoothing. Narrow, derived and correlated signals remain information
to inspect; they do not have to establish a particular use before being retained.
The historical coordinate names are intended interpretations of these indices.
They do not make the indices physical energy, mutual information or
response-distribution entropy.

Read provenance alongside a value: `primary_eisv_source` identifies behavioral
versus ODE-fallback metrics, and telemetry envelopes carry `measurement_source`.
Physical sensor inputs can also enter the behavioral smoothing stage, so
`primary_eisv_source=behavioral` identifies that output path, not necessarily a
behavioral-sensor input.
The legacy coherence producer has a `coherence_source`/`coherence_role` stamp.
Per-coordinate tags such as `e_source`, `i_source` and `s_source` are attached
when the opt-in grounding stage runs; they are not universal. Legacy rows without
an envelope remain unclassified rather than acquiring an inferred source.

Whether these summaries improve a particular diagnosis or decision beyond
simpler inputs is a separate evaluation question. For outcome prediction, the
[falsifiability harness](../REVIEWER_GUIDE.md#falsifiability-inspect-the-registered-evidence)
tests ranking (AUC), calibration (Brier), and the selection cost of reporting the
best of several candidates. In the frozen 2026-08-09 trusted-anchor matrix,
every overall scope/window/lead slice is `NON_DETECTION` against the
best-of-candidates null (selective p = 0.070–0.567). Unadjusted lift sometimes
appears, usually in `prior_risk`, `prior_s`, or dispersion, but none clears
p < 0.05 after that correction. There is **no demonstrated prevention**. A
`NON_DETECTION` slice does not separate "no association" from "too small a cohort
to see one", and the
[power audit](../operations/falsifiability-power-audit-2026-08-23.md) does not
settle which applies: it withdrew its original power table and states that it
"cannot quantify what it could have detected" for the frozen cohort. Inspect the
[frozen rows and provenance](../operations/eisv-ablation-frozen-2026-08-09.md)
and that audit; the reviewer guide also provides synthetic controls. Do not
rerun deployment outcome discrimination between registered reads: the
[outcome-grounding stop rule](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)
reserves it for the registered execution.

For optional intuition (not a spec),
[`../essays/tonality-metaphor.md`](../essays/tonality-metaphor.md) uses a bounded
tonal analogy for self-relative measurement and names where the comparison stops.

### Worked example: a signal moves while the action stays the same

This synthetic example calls the sensor, smoother, behavioral assessment and
final decision functions directly. It holds the supplied histories and ODE
structural state fixed, warms the smoother for 40 updates, then raises the tool
error rate from 0 to 1 for ten updates. It does not append the selected decisions
back into history or run the full handler/controller loop. It reads no live data
and writes no check-ins.

With these inputs, the E observation changes from 0.85975 to 0.70975. The tool
term has weight 0.15, so changing its success fraction from 1 to 0 subtracts
0.15. E's smoothing rate is 0.12; its first step moves by about 0.018. I and S
observations remain 0.69 and 0.1575. V follows its own smoothing of the raw E−I
gap, at rate 0.10.

| Update | Tool error rate | E observation | Smoothed E | Smoothed V | Behavioral risk |
|---|---|---|---|---|---|
| 40 | 0 | 0.859750 | 0.859712 | 0.169700 | 0 |
| 41 | 1 | 0.709750 | 0.841717 | 0.154705 | 0 |
| 50 | 1 | 0.709750 | 0.751515 | 0.072034 | 0 |
| 51 | 0 | 0.859750 | 0.764503 | 0.081806 | 0 |
| 90 | 0 | 0.859750 | 0.859099 | 0.168306 | 0 |

Every displayed update has verdict `safe` and policy `proceed` / `approve`.
The trace records the perturbation and its decay even though this assessment
and fixed structural state select the same action. Responsivity is visible;
whether acting on that movement would help is a further question. This example
does not prescribe how the full system should respond to real tool failures.

<details>
<summary>Reproduce from the repository root in a fresh Python process</summary>

Use the repository's installed Python environment. The snippet clears
`UNITARES_*` overrides only inside that process to illustrate the defaults.
Values in the table are rounded to six decimal places.

```python
import os
from types import SimpleNamespace

for key in list(os.environ):
    if key.startswith("UNITARES_"):
        del os.environ[key]

from src.behavioral_sensor import compute_behavioral_sensor_eisv
from src.behavioral_state import BehavioralEISV
from src.behavioral_assessment import assess_behavioral_state
from src.monitor_decision import make_decision

inputs = dict(
    decision_history=["approve"] * 10, coherence_history=[0.5] * 10,
    regime_history=["EXPLORATION"] * 10, E_history=[0.8] * 10,
    I_history=[0.7] * 10, S_history=[0.2] * 10, V_history=[0.1] * 10,
    calibration_error=0.25, drift_norm=0.2, complexity_divergence=0.15,
)
state = BehavioralEISV()
structural = SimpleNamespace(
    E=0.8, I=0.8, S=0.1, V=0.0, coherence=0.5,
    coherence_history=[0.5] * 10, coherence_role="ode_control_feedback",
    void_active=False, risk_history=[], locked_persistence_count=0,
)
for step in range(1, 91):
    error = 1.0 if 41 <= step <= 50 else 0.0
    obs = compute_behavioral_sensor_eisv(**inputs, tool_error_rate=error)
    state.update(obs["E"], obs["I"], obs["S"])
    assessment = assess_behavioral_state(state, rho=0.0, continuity_energy=0.0)
    decision = make_decision(structural, assessment.risk, assessment.verdict)
    if step in (40, 41, 50, 51, 90):
        values = (obs["E"], state.E, state.V, assessment.risk)
        print(step, error, *(round(x, 6) for x in values),
              assessment.verdict, decision["action"], decision["sub_action"])
```

</details>

---

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)
