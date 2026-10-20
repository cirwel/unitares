# How EISV Is Actually Computed

**EISV computes a compact, time-smoothed record of runtime observations and controller history.** The behavioral path blends supplied features into E, I and S, smooths them, and derives V from the E/I observation gap. Assessment compares that state with thresholds and, after warmup, an adaptive baseline. Policy uses the assessment alongside other decision inputs.

## What the signals record

These are operational definitions of the current behavioral path. The formulas below specify the weights, defaults and optional inputs.

| Output | What contributes to it | What a change records |
|---|---|---|
| **E** | Prior governance decisions, legacy controller coherence level, complexity divergence, and available outcome, continuity and tool features | A change in this weighted history of observations and judgments |
| **I** | Calibration error, legacy controller coherence trend, and available outcome-score consistency, continuity and tool-variety features | A change in this calibration/consistency blend; the deployed calibration input is fleet-scoped |
| **S** | Drift, regime transitions, complexity divergence, and available continuity and tool features | A change in the blend of deviations and activity features |
| **V** | A separately smoothed difference between raw E and I observations | The direction and persistence of the gap between those two indices |

The observations, their derived summaries, and the resulting policy actions are distinct records. A controller-derived input is still information about the running system. Because prior decisions enter E, a policy response can change later E even when supplied task and tool features are unchanged. This feedback reflects the controller's response; it does not independently confirm the condition that prompted it. Retain that provenance when explaining a later reading. A derived or correlated signal need not be a new independent observation to be retained and inspected.

This resolves the descriptive question: **the instrument records specified features and their evolution.** Which changes diagnose a particular problem, whether this compression helps a consumer, and which actions should follow are separate evaluation questions. An inconclusive outcome-prediction study does not erase the recorded signal; a responsive signal does not settle those other questions. The [claim ledger](EVIDENCE_AND_LIMITS.md#claim-ledger) holds the evidence for benefit claims.

The historical names describe intended interpretations: productive capacity (E), information integrity (I), entropy (S), and valence (V). The current formulas define weighted indices, not physical energy, mutual information or response-distribution entropy. Those information-theoretic quantities remain research targets in [Paper v6](https://github.com/cirwel/unitares-paper-v6). Reading the implemented indices does not depend on establishing that interpretation.

Provenance identifies the producer: `primary_eisv_source` in the metrics (`behavioral` vs `ode_fallback`), `measurement_source` in the telemetry envelope, and an unconditional `coherence_source`/`coherence_role` stamp naming the legacy ODE producer. Per-coordinate `e_source`/`i_source`/`s_source` tags are attached when the opt-in grounding stage runs (`UNITARES_GROUNDING_SHADOW`/`_APPLY`, both default off — `src/mcp_handlers/updates/enrichments.py`, `run_grounding_stage`). The definitions here describe the behavioral path; inspect those source fields before interpreting a particular reading. Physical sensor inputs can enter the same smoothing stage, so `primary_eisv_source=behavioral` identifies that output path, not the origin of its inputs; inspect input provenance separately.

This document is the owner of the EISV instrument: its inputs, smoothing, risk assessment, margin, and the evidence about what it does and does not show. The [architecture](UNIFIED_ARCHITECTURE.md) places it in the system, and [manual §5.3](manual/05-reading-the-signals.md#53-the-policy-action) owns how an agent handles the policy action it returns.

## Pipeline (primary, verdict-driving path)

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(decisions,     (behavioral_         (behavioral_    (behavioral_          proceed
 calibration,    sensor.py)           state.py)       assessment.py)        (+guide) /
 drift, tools)                                                               pause
```

The dynamical-systems model (`governance_core/`, the ODE; older docs use a
retired physics-flavoured name for it, which is not a claim about physical
quantities) is
**not the direct verdict owner** by default: behavioral assessment owns the
post-warmup assessment. The final decision also uses ODE state for structural
basin, void and coherence checks (`src/monitor_decision.py`). The behavioral sensor
still gives the legacy ODE `C(V)` scalar 25–30% of E and 30–40% of I, and also
uses ODE-derived regime/history inputs. The old “does NOT drive verdicts” comment
therefore describes authority, not causal independence. This residual coupling
is compatibility debt and must be shadow-evaluated before removal because it
changes the E/I distribution and every downstream baseline.

### One check-in, end to end

```
  Any AI Agent                                    Unitares Server
  (Claude Code, Codex,                            (port 8767)
   Hermes, any MCP client)
  ============================                    ========================

  Do work                     HTTP POST /mcp/
  Report: what you did,      ------------------->
    complexity, confidence    process_agent_update   Behavioral EISV
                                                      +---------------------+
                                                      | observations +      |
                                                      | controller history  |
                                                      | -> blends + EMA     |
                                                      | -> risk assessment  |
                                                      | (baseline + floors) |
                                                      +----------+----------+
                                                                 |       ODE state feeds
                                                            policy <---- structural checks
                                                                 |       and later observations
                                                            action + margin
                                                                 |
                              <----------------------------------+
                              {"action": "proceed",
                               "margin": "comfortable",
                               "guidance": "..."}
```

The behavioral path blends observations and controller history into smoothed
indices. Risk assessment uses fixed thresholds and, after warmup, the agent's
own baseline. The ODE supplies additional structural decision inputs and feeds
later behavioral observations.

**Where the code lives.** The grounding path lives in `src/dual_log/`,
`src/behavioral_sensor.py`, `src/behavioral_state.py`, and
`src/behavioral_assessment.py`; final policy is `src/monitor_decision.py`. The
ODE engine lives in `governance_core/` (`dynamics.py`) at the top level of this
repo — pure Python, no separate install (it was a private compiled wheel,
`unitares-core`, until it was folded back in 2026-04). `src/grounding/free_energy.py`
is tiered, with the free-energy (FEP) tier explicitly stubbed.

## Step 1 — Observations (`src/behavioral_sensor.py`)

For non-embodied agents (the common case), three observations are computed from governance observables. On the MCP check-in path the sensor actually runs in the handler (`src/mcp_handlers/updates/phases.py`), which injects the result as `sensor_eisv` with `sensor_eisv_source="behavioral"`; the monitor recomputes it internally only as a fallback. Embodied agents (e.g. the Raspberry-Pi deployment) instead supply hardware `sensor_eisv` directly (`governance_monitor.py`, the `sensor_eisv` intake path).

**Timing on the live path.** The handler computes the observation before the monitor processes the check-in (`prepare_unlocked_inputs`). It reads `drift_norm`, `complexity_divergence` and the continuity E/I/S inputs from what the monitor stored at the previous check-in, so those inputs lag by one check-in. Tool-usage statistics, recent outcomes and the calibration scalar are read at the current check-in. The monitor-internal fallback reads the current check-in's drift and continuity instead. A check-in's own complexity report and response structure therefore first reach E, I and S at the next check-in (contract ledger row 54).

**E_obs** — decision, controller-level and complexity blend (`_compute_E`):
```
decision_e = exp-weighted (α=0.3) mean of verdict scores over last 10 decisions
             {proceed/approve: 1.0, guide: 0.7, revise/reflect: 0.5, pause/reject
              and every pause sub-action (risk_/basin_/coherence_/void_pause,
              cirs_block): 0.0; unrecognized strings: 0.5}
coh_e      = mean legacy_tanh_v ODE control feedback remapped [0.35, 0.65] → [0.3, 0.9]
cal_e      = clamp(1 − complexity_divergence, 0.3, 1.0)
outcome_e  = 0.3 + 0.6·(share of recorded outcomes not flagged is_bad)
E_obs      = 0.40·decision_e + 0.30·coh_e + 0.30·cal_e         # no outcomes
           = 0.35·decision_e + 0.25·coh_e + 0.20·cal_e + 0.20·outcome_e   # with ≥3 outcomes
```
(What lands in `decision_history` is the sub-action when one exists, so the live vocabulary is the `monitor_decision.py` sub-action set, not just the coarse names.)

**I_obs** — calibration, controller-trend and consistency blend (`_compute_I`):
```
cal_I         = clamp(1 − calibration_error, 0, 1)
coh_I         = split-half trend of legacy_tanh_v ODE control feedback: second-half mean
                minus first-half mean over last 10, mapped 0.6 + 3·diff, clamped [0.3, 0.9]
consistency_I = max(0.3, 1 − 4·var(outcome scores))
I_obs         = 0.6·cal_I + 0.4·coh_I                                   # no outcomes
              = 0.50·cal_I + 0.30·coh_I + 0.20·consistency_I            # with ≥3 finite outcome scores
```
`calibration_error` has two producers. On the live handler path it is the mean per-bin |declared confidence − observed trajectory health| from the strategic calibration tracker (`get_mean_calibration_error` → `src/calibration.py`, bins with ≥5 samples). These are lifetime **fleet** bins; the agent-scoped candidate is collected separately as measurement-only (`src/mcp_handlers/updates/context.py`). This is not an error measurement for the current claim. The monitor-internal fallback instead passes the drift vector's `calibration_deviation` (`monitor_drift.py`): worst populated tactical bin's *overconfidence* only. Agent-reported `ethical_drift` is telemetry and is never blended into the vector.

The outcome-consistency term records variance: constant success scores and constant failure scores both have zero variance. The derivation marks this term `health_evidence=false`; outcome success enters E separately. Read consistency and success as separate properties. Matching reported confidence to suppressed performance can still resemble honest lower capability in the monitored trace; see the [threat model](SCOPE_AND_THREAT_MODEL.md) and the [concealment boundary fixture](../tests/test_calibrated_capability_concealment.py).

**S_obs** — drift, regime-transition and complexity blend (`_compute_S`):
```
drift_s  = min(1, drift_norm · 1.5)
regime_s = regime transitions / (window − 1) over last 10   (instability)
cd_s     = min(1, complexity_divergence)
S_obs    = clamp(0.40·drift_s + 0.35·regime_s + 0.25·cd_s, 0.05, 1)
```

**The drift vector** (`src/monitor_drift.py`, `compute_drift_vector`). Four observable signals define the drift vector whose norm feeds entropy (`drift_norm` above):

| Signal | What it measures |
|--------|-----------------|
| Calibration deviation | Worst populated tactical bin's overconfidence (expected minus observed accuracy, floored at zero) |
| Complexity divergence | Self-reported complexity vs system estimate |
| Coherence deviation | How far coherence has moved from baseline |
| Stability deviation | Decision-pattern instability (`1 − decision_consistency`) |

No human oracle is needed for runtime drift estimation. Independent exogenous outcomes still matter for calibration and research validation.

Missing inputs fall back to fixed neutral defaults rather than dropping the term (`cal_I` 0.75, `drift_norm` 0.2, complexity divergence 0.15 in E / 0.1 in S, both `coh` terms 0.6), so a signal outage reads as "unremarkable", not as zero.

Optional blends (when those signals exist): continuity-log inputs (≤20%), tool error-rate (≤15%), tool velocity / unique-tools ratio (≤10%). The weights are hand-set engineering parameters.

`coh_e` and `coh_I` are especially important to interpret honestly. They were
introduced to reduce E saturation and differentiate agents while `coherence`
still meant `C(V_ODE)`. The later behavioral-EISV migration promoted E/I/S/V
but left the coherence producer behind, so these terms now carry a demoted ODE
controller signal into the behavioral vector. The derivation tags that input;
the live weights remain as specified above.

There is a second compatibility dependency in `src/confidence.py`: when callers
omit confidence, 55% of the fallback estimate's base is the same legacy
`C(V_ODE)` scalar. That estimate is recorded into calibration history, whose
aggregate overconfidence penalty can later raise ODE S. Responses now disclose
the source, role, weight, and `coherence_is_health_evidence=false` under
`confidence_reliability`; the formula is not silently reweighted because its
distribution and downstream calibration need prospective outcome comparison.

## Step 2 — Smoothing, and what V really is (`src/behavioral_state.py`)

State is an EMA of the observations. The per-dimension α values are E 0.12, I 0.08, S 0.15, V 0.10; during the first 10 updates a bootstrap boost of up to +0.5 is added on top, decaying linearly, so a fresh agent catches up fast and then settles to the configured smoothing:
```
E = (1−α)·E + α·E_obs        I = (1−α)·I + α·I_obs        S = (1−α)·S + α·S_obs
```

**V is derived from E/I observation history.** It is the EMA-smoothed E−I observation gap (`behavioral_state.py`, `_raw_valence()` fed into the `BehavioralEISV.update()` EMA). Since `V_FORMULA_VERSION = 2` the input is the gap of the **raw observations**, so V is smoothed exactly once:
```
raw_v = E_obs − I_obs
V     = (1−α_V)·V + α_V·raw_v
```
(v1 took `E − I` of the already-EMA'd state — double smoothing, more lag. The v2 flip was gated on a trace replay against real check-in traces, 2026-06-19: zero verdict flips; stale converged baselines are re-seeded on load. See `scripts/dev/validate_valence_formula.py` and `_reseed_v_baseline`.)

There are four output coordinates built from three blended observation streams. V adds a distinct temporal summary, not another external observation: its smoothing rate differs from those of E and I, so it is not generally recoverable by subtracting their current smoothed values. Positive V records a smoothed E_obs-above-I_obs gap; negative V records the reverse. "Running hot" and "running careful" are interpretations of that gap, not additional measured facts.

(There is a second `_compute_V` — a slope-plus-level formula — in `behavioral_sensor.py`. It does **not directly supply behavioral V**: `governance_monitor.py` passes only E/I/S observations to `self._behavioral_state.update()`, which recomputes V as the EMA of E_obs−I_obs above. The sensor's V reaches the ODE's spring coupling when the source is coupling-allowed. That ODE state can affect later behavioral inputs and the final decision's structural checks; the two V producers have different roles in the coupled system.)

## Step 3 — Residuals: proprioception, not prosecution

The operational question is not "did EISV decide this was bad?" It is "how far
has this running process moved from a grounded reference for itself?" In roadmap
terms:

```text
reference_t = blend(agent_baseline_t, class_anchor; w(grounding))
residual_t  = measurement_t - reference_t
```

The current verdict path implements the live version of that posture with
self-relative z-scores after warmup, absolute safety floors, and a basin-health
gate. External evidence (tests, exit codes, tool results, deployments, review
labels) calibrates the signal and can license baseline recentering; it is not the
identity of EISV itself.

## Step 4 — Policy action (`src/behavioral_assessment.py`)

*"No sigmoid/phi black box. Each risk component has a clear source and weight. Assessment is auditable — you can trace exactly why a verdict was issued."* (module docstring.)

- Total risk = sum of named components, each with an explicit weight — `low_E` (0.30), `low_I` (0.30), `high_S` (0.20), `high_V` on |V| (0.20), `adversarial_rho` (0.15), `high_CE` (0.10) — plus a small improving-trend bonus (−0.05 when both E and I trend up); clamped to [0, 1].
- **Check-ins 1–2**: behavioral confidence is below 0.3, so the live verdict is
  owned by the Φ cold-start prior. The behavioral assessment is telemetry-only.
- **Check-ins 3–24**: the behavioral assessment is authoritative, using fixed
  universal thresholds because the agent-specific baseline is not ready.
- **From check-in 25 with the current constants**: `baseline_confidence >= 0.8`
  against the 30-update target, enabling self-relative (Welford) z-score
  deviations from the agent's own behavioral baseline. The coordinates
  themselves remain weighted indices, not z-scores.
- **Absolute safety floors always apply**, overriding the baseline — but read
  that precisely: a floor overrides the basin-gated *component* (via a
  per-component `max`), not the *verdict*. The largest single-floor contribution
  equals its component weight (0.30 for E or I, 0.20 for S or |V|), and each is
  below the 0.35 safe/caution threshold. S is genuinely independent, so S=1.0
  alone still reads `safe` at risk 0.20. E and I are coupled to V through
  `V = EMA(E - I)`: a sustained one-sided E or I collapse normally widens that
  gap and can also fire `high_V`, while a lone E/I breach remains `safe` only in
  the reachable corridor where the other dimension is reduced enough to keep
  |V| at or below its ceiling. E and I both near zero force `high-risk`
  (0.30 + 0.30 = 0.60) through the floors alone. For non-embodied agents the
  Step-1 component clamps compress this further (E_obs ≥ ~0.18, I_obs ≥ 0.12),
  capping the all-floors-at-reachable-worst risk at ~0.50 (`caution` → guide →
  proceeds). Verdict-level structural pauses for such states come from the
  diagnostic-ODE side of `monitor_decision.py` (basin/void/coherence pauses),
  which tracks the behavioral signal only through the sensor spring coupling,
  with lag. Whether a lone floor breach should force at least `caution` is an
  open calibration question — issue #1995.
- Issue #1995 is measured without changing that policy. Every check-in adds a
  versioned `behavioral.absolute_floor_observation.v1` object to the full result
  at `behavioral.assessment.absolute_floor_observation` and to the existing
  `auto_attest` audit row at
  `details.behavioral.absolute_floor_observation`. The row snapshots the floor
  and verdict thresholds, breached dimensions, behavioral authority context,
  and whether a breach coincided with a `safe` behavioral verdict. It is
  explicitly `telemetry_only` with `policy_effect: none`; no risk, policy, or
  enforcement input consumes it. The exception is the default-off
  `UNITARES_FLOOR_BREACH_CAUTION_APPLY` floor (issue #1995): a row whose
  behavioral verdict it raised to `caution` is labelled
  `measurement_role: verdict_floor`, `policy_effect: behavioral_verdict_raised`.
  The label records only that the floor raised the behavioral verdict. Whether
  the final decision changed is read from the rest of the same `auto_attest`
  audit row: `details.reason` names the deciding branch ("Proceeding mindfully"
  for a caution verdict) and `details.unitares_verdict` is the final verdict.
  Three things it cannot settle: Φ's own verdict under a `phi_floor` source is
  not kept, the CIRS soft-dampen and fast-trip paths are ambiguous, and a
  warmup pause the floor kept looks identical to one it did not (the grace
  runs after this row is written; only the absence of a
  `warmup_structural_suppressed` event shows the pause was kept). Zero-breach rows remain present so the audit
  has a denominator, failed evaluation is `evaluated: false` (unknown, not
  zero), and dry-run rows use `measurement_scope: simulation` with
  `eligible_for_production_counter: false`. A production count therefore
  filters for `evaluated` and `eligible_for_production_counter`; use
  `behavioral_verdict_authoritative` as an additional cohort filter when the
  question is specifically about final behavioral authority rather than all
  behavioral assessments.
- Self-relative deviation risk is **gated by absolute basin health** (issue #689): inside the healthy basin a deviation from your own norm is treated as information, not danger; the gate opens only as a dimension leaves the basin toward its absolute floor. (This replaced a flat σ-floor that was false-pausing ultra-stable agents as the *principled* fix; the floor survives as defense-in-depth, bounding the z-score denominator in the boundary region — `MIN_MEANINGFUL_EISV_STD`.)

Internally the assessment emits a `safe` / `caution` / `high-risk` label; that drives the binary `proceed` / `pause` action (qualified by a sub-action), which the agent reads back as `action_summary.action` (`proceed` or `pause`) with a `sub_action` such as `guide`, a named pause cause, or the legacy `reject` label.

Responses include `margin` indicating proximity to decision thresholds (risk,
void or coherence), distinct from basin membership: `comfortable` / `tight`,
`warning` (a threshold just crossed, < 0.1 past), `critical` (≥ 0.1 past), or
`settling` (fewer than 3 check-ins of history). See `config/governance_config.py`.

## Edge regime: oscillation near a threshold

This behavior at the edge of the verdict path is deliberate and easy to miss when reading the code. (The other edge posture, fail-open assessment failure, belongs to the check-in handler and is described in the [architecture](UNIFIED_ARCHITECTURE.md#the-check-in-and-policy-action).)

**Oscillation / churn near a threshold.** There is no deadband on the risk cut itself — a single check-in crossing a threshold can change the action. The EISV inputs are EMA-smoothed before scoring (`src/behavioral_state.py`, per-dimension α ≈ 0.08–0.15). The live oscillation layer is CIRS v2, the `AdaptiveGovernor` (`governance_core/adaptive_governor.py`, run from `src/monitor_cirs.py`; `ADAPTIVE_GOVERNOR_ENABLED = True`). It tracks verdict flips and an oscillation index over a window of 10 (`flip_threshold=4`, `oi_threshold=2.5`) for observability only: in v2, resonance does not block. Its `hard_block` comes only from the fixed `tau_floor=0.25` coherence and `beta_ceiling=0.80` risk bounds; the recorded risk-ceiling blocks came from short-lived agents before warmup, and the coherence floor has never fired (contract ledger rows 18, 30 and 42). v2 emits no `soft_dampen`. So churn near a boundary is not damped by a hold-band or by a settle-pause. The legacy v0.1 detector in `src/cirs.py` (`window=8`, `flip_threshold=3`, `oi_threshold=3.0`, `soft_dampen` and resonance `hard_block`) runs only when the governor is disabled.

## Deployed vs. target, at a glance

| Coord | Deployed today (tier: heuristic / resource) | Target semantics (Paper v6) |
|---|---|---|
| **E** | weighted blend of decision-success, complexity-calibration, outcomes, and legacy ODE control-feedback level | negative variational free energy (−F) |
| **I** | calibration accuracy + legacy ODE control-feedback trend (+ outcome consistency) | mutual information I(context; response) |
| **S** | drift-norm + regime instability + complexity divergence | response-distribution entropy H |
| **V** | EMA-smoothed E_obs−I_obs imbalance (derived) | accumulated free-energy residual |

The paper states this plainly: the deployed resource-rate form *"is **not** equivalent to −F and does not approximate it under stationarity in any formal sense."* The target forms become instrumentable only when the inference layer exposes the quantities they require (e.g. token-level logprobs for entropy). Until then, the heuristic is the claim and the information-theoretic form is the direction.

## Inspecting the telemetry chain

New check-ins persist a versioned `eisv.telemetry.v1` envelope alongside the
append-only state row. It keeps measurement, bounded derivation inputs, policy
evaluation, and enforcement result as separate objects; creating the envelope
does not change any score or action. See
[`ontology/eisv-telemetry-envelope-v1.md`](ontology/eisv-telemetry-envelope-v1.md)
for the schema, privacy bounds, export surfaces, and legacy-row behavior.

## Don't take this document's word for it

Whether these numbers add useful signal beyond simple baselines is an **open,
measured** question — not an assumption. The [Reviewer Guide's falsifiability
harness](REVIEWER_GUIDE.md#falsifiability-inspect-the-registered-evidence)
scores EISV/prior-state features against `previous_outcome_bad` on ranking (AUC)
and calibration (Brier), then compares the selected best candidate with a
best-of-candidates permutation null. In the frozen 2026-08-09 trusted-anchor
matrix, every overall scope/window/lead slice is `NON_DETECTION` (selective
p = 0.070–0.567), not a demonstrated absence or refutation. Some unadjusted point
estimates improve both metrics, but none separates from the selection-aware null
at p < 0.05; the selected features are
usually `prior_risk`, `prior_s`, or dispersion rather than the full
decomposition. No prevention is demonstrated. That is a non-detection, and
`scripts/analysis/ablation_power_probe.py` measures what a cohort of that shape
could have detected in the first place — see the
[power audit](operations/falsifiability-power-audit-2026-08-23.md). Inspect both
yourself — the frozen rows live in
[`operations/eisv-ablation-frozen-2026-08-09.md`](operations/eisv-ablation-frozen-2026-08-09.md),
and the synthetic negative-control path runs on a fresh clone with no deployment
DB. What you should **not** do is re-run the live discrimination matrix against
a deployment DB between registered reads: the
[outcome-grounding stop rule](proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)
reserves that for the registered execution, and an interim re-run is a selective
re-read, not reviewer hygiene.
