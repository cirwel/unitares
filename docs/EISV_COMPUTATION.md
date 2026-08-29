# How EISV Is Actually Computed

**The formulas the running code computes, beside the information-theoretic semantics the paper targets.**

UNITARES is described with thermodynamic and information-theoretic language (energy, entropy, valence, coherence; in [Paper v6](https://github.com/cirwel/unitares-paper-v6), `S` as response-distribution entropy, `I` as mutual information, `E` as negative variational free energy). That vocabulary is the **target semantics** — the model the project is working toward and tests honestly. It is **not** what the running code computes today.

What the deployed system actually computes is the honest, defensible claim: **EISV is a set of auditable heuristic blends over observable agent behavior, EMA-smoothed, with verdicts from a transparent weighted-threshold model.** No entropy, mutual information, or free energy is computed on the primary path. Provenance travels with every check-in so a heuristic is never laundered as a measurement — at the vector level: `primary_eisv_source` in the metrics (`behavioral` vs `ode_fallback`), `measurement_source` in the telemetry envelope, and an unconditional `coherence_source`/`coherence_role` stamp naming the legacy ODE producer. Per-coordinate `e_source`/`i_source`/`s_source` tags exist but are attached only when the opt-in grounding stage runs (`UNITARES_GROUNDING_SHADOW`/`_APPLY`, both default off — `src/mcp_handlers/updates/enrichments.py`, `run_grounding_stage`). Think of this path as online proprioception: the useful signal is how the agent's current state differs from a grounded reference for its own trajectory, not whether EISV has handed down an outcome verdict. This document gives the exact formulas with source references, so you can judge — or reproduce — them.

## Pipeline (primary, verdict-driving path)

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(decisions,     (behavioral_         (behavioral_    (behavioral_          proceed/
 calibration,    sensor.py)           state.py)       assessment.py)        guide/
 drift, tools)                                                               pause/reject
```

The dynamical-systems / thermodynamic model (`governance_core/`, the ODE) is
**not the direct verdict owner** by default: behavioral assessment owns the
post-warmup verdict. It is not fully parallel, however. The behavioral sensor
still gives the legacy ODE `C(V)` scalar 25–30% of E and 30–40% of I, and also
uses ODE-derived regime/history inputs. The old “does NOT drive verdicts” comment
therefore describes authority, not causal independence. This residual coupling
is compatibility debt and must be shadow-evaluated before removal because it
changes the E/I distribution and every downstream baseline.

## Step 1 — Observations (`src/behavioral_sensor.py`)

For non-embodied agents (the common case), three observations are computed from governance observables. On the MCP check-in path the sensor actually runs in the handler (`src/mcp_handlers/updates/phases.py`), which injects the result as `sensor_eisv` with `sensor_eisv_source="behavioral"`; the monitor recomputes it internally only as a fallback. Embodied agents (e.g. the Raspberry-Pi deployment) instead supply hardware `sensor_eisv` directly (`governance_monitor.py`, the `sensor_eisv` intake path).

**E_obs** — productive capacity (`_compute_E`):
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

**I_obs** — claims-match-results (`_compute_I`):
```
cal_I         = clamp(1 − calibration_error, 0, 1)
coh_I         = split-half trend of legacy_tanh_v ODE control feedback: second-half mean
                minus first-half mean over last 10, mapped 0.6 + 3·diff, clamped [0.3, 0.9]
consistency_I = max(0.3, 1 − 4·var(outcome scores))
I_obs         = 0.6·cal_I + 0.4·coh_I                                   # no outcomes
              = 0.50·cal_I + 0.30·coh_I + 0.20·consistency_I            # with ≥3 finite outcome scores
```
`calibration_error` is a claimed-vs-observed deviation, not literally |confidence − success| of the current claim — and it has two producers. On the live handler path it is the mean per-bin |declared confidence − observed trajectory health| from the strategic calibration tracker (`get_mean_calibration_error` → `src/calibration.py`, bins with ≥5 samples). The monitor-internal fallback instead passes the drift vector's `calibration_deviation` (`monitor_drift.py`): worst populated tactical bin's *overconfidence* only, blended 70/30 with agent-reported drift when the agent sends any.

**S_obs** — drift from own normal behavior (`_compute_S`):
```
drift_s  = min(1, drift_norm · 1.5)
regime_s = regime transitions / (window − 1) over last 10   (instability)
cd_s     = min(1, complexity_divergence)
S_obs    = clamp(0.40·drift_s + 0.35·regime_s + 0.25·cd_s, 0.05, 1)
```

Missing inputs fall back to fixed neutral defaults rather than dropping the term (`cal_I` 0.75, `drift_norm` 0.2, complexity divergence 0.15 in E / 0.1 in S, both `coh` terms 0.6), so a signal outage reads as "unremarkable", not as zero.

Optional small blends (when those signals exist): continuity-log inputs (≤20%), tool error-rate (≤15%), tool velocity / unique-tools ratio (≤10%). The weights are hand-set, not derived — that is the honest status of the current estimator.

`coh_e` and `coh_I` are especially important to interpret honestly. They were
introduced to reduce E saturation and differentiate agents while `coherence`
still meant `C(V_ODE)`. The later behavioral-EISV migration promoted E/I/S/V
but left the coherence producer behind, so these terms now carry a demoted ODE
controller signal into the allegedly behavioral vector. The current patch tags
that input; it does not silently reweight the live estimator.

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

**V is not an independent dimension.** It is the EMA-smoothed E−I imbalance (`behavioral_state.py`, `_raw_valence()` fed into the `update()` EMA). Since `V_FORMULA_VERSION = 2` the input is the gap of the **raw observations**, so V is smoothed exactly once:
```
raw_v = E_obs − I_obs
V     = (1−α_V)·V + α_V·raw_v
```
(v1 took `E − I` of the already-EMA'd state — double smoothing, more lag. The v2 flip was gated on a trace replay against real check-in traces, 2026-06-19: zero verdict flips; stale converged baselines are re-seeded on load. See `scripts/dev/validate_valence_formula.py` and `_reseed_v_baseline`.)

So "four-dimensional state vector" is really three observed axes (E, I, S) plus a derived imbalance readout (V). V is surfaced separately because its **sign** is operationally actionable — positive = running hot (energetic but claims outrun results), negative = running careful (coherent but low progress) — not because it carries independent information.

(If you grep the codebase you will find a second `_compute_V` — a slope-plus-level formula — in `behavioral_sensor.py`. It is **unused on the verdict path**: `governance_monitor.py` passes only E/I/S observations to `self._behavioral_state.update()`, which recomputes V as the EMA of E_obs−I_obs above. The sensor's V does still reach the diagnostic ODE's spring coupling when the source is coupling-allowed; the verdict-driving V is the one described here.)

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
  against the 30-update target, enabling self-relative z-score deviations from
  the agent's own behavioral baseline.
- **Absolute safety floors always apply**, overriding the baseline — but read
  that precisely: a floor overrides the basin-gated *component* (via per-component
  `max()`), not the *verdict*. The largest single-floor contribution equals its
  component weight (0.30 for E or I, 0.20 for S or |V|), and each of those is
  below the 0.35 safe/caution threshold — so one dimension at its absolute worst
  still reads `safe` (E=0.0 alone: risk 0.30), and only E and I both near zero
  force `high-risk` (0.30 + 0.30 = 0.60) through the floors alone. For
  non-embodied agents the Step-1 component clamps compress this further
  (E_obs ≥ ~0.18, I_obs ≥ 0.12), capping the all-floors-at-reachable-worst risk
  at ~0.50 (`caution` → guide → proceeds). Verdict-level structural pauses for
  such states come from the diagnostic-ODE side of `monitor_decision.py`
  (basin/void/coherence pauses), which tracks the behavioral signal only through
  the sensor spring coupling, with lag. Whether a lone floor breach should force
  at least `caution` is an open calibration question — issue #1995.
- Self-relative deviation risk is **gated by absolute basin health** (issue #689): inside the healthy basin a deviation from your own norm is treated as information, not danger; the gate opens only as a dimension leaves the basin toward its absolute floor. (This replaced a flat σ-floor that was false-pausing ultra-stable agents as the *principled* fix; the floor survives as defense-in-depth, bounding the z-score denominator in the boundary region — `MIN_MEANINGFUL_EISV_STD`.)

Internally the assessment emits a `safe` / `caution` / `high-risk` label; that drives the binary `proceed` / `pause` action (qualified by a sub-action), which the agent reads back as `proceed` / `guide` / `pause` / `reject`.

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
harness](REVIEWER_GUIDE.md#falsifiability-inspect-the-registered-evidence-dont-trust-this-doc)
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
[outcome-grounding stop rule](proposals/eisv-outcome-grounding-stop-rule-v0.md)
reserves that for the registered execution, and an interim re-run is a selective
re-read, not reviewer hygiene.
