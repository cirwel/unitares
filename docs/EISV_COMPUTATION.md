# How EISV Is Actually Computed

**The formulas the running code computes, beside the information-theoretic semantics the paper targets.**

UNITARES is described with thermodynamic and information-theoretic language (energy, entropy, valence, coherence; in [Paper v6](https://github.com/cirwel/unitares-paper-v6), `S` as response-distribution entropy, `I` as mutual information, `E` as negative variational free energy). That vocabulary is the **target semantics** — the model the project is working toward and tests honestly. It is **not** what the running code computes today.

What the deployed system actually computes is the honest, defensible claim: **EISV is a set of auditable heuristic blends over observable agent behavior, EMA-smoothed, with verdicts from a transparent weighted-threshold model.** No entropy, mutual information, or free energy is computed on the primary path. Every coordinate carries a provenance tier tag (`e_source`, `s_source`, …) so a heuristic is never laundered as a measurement. Think of this path as online proprioception: the useful signal is how the agent's current state differs from a grounded reference for its own trajectory, not whether EISV has handed down an outcome verdict. This document gives the exact formulas with source references, so you can judge — or reproduce — them.

## Pipeline (primary, verdict-driving path)

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(decisions,     (behavioral_         (behavioral_    (behavioral_          proceed/
 calibration,    sensor.py)           state.py)       assessment.py)        guide/
 drift, tools)                                                               pause/reject
```

The dynamical-systems / thermodynamic model (`governance_core/`, the ODE) runs **in parallel and does not drive verdicts** by default (`governance_monitor.py`, the "does NOT drive verdicts" guard comment: *"The ODE engine runs in parallel but does NOT drive verdicts… Primary verdicts come from behavioral assessment (EMA + z-score deviations)."*). It supplies the phi objective, regime detection, and historical continuity — the research lens, not the control loop.

## Step 1 — Observations (`src/behavioral_sensor.py`)

For non-embodied agents (the common case), three observations are computed from governance observables. Embodied agents (e.g. the Raspberry-Pi deployment) instead supply hardware `sensor_eisv` directly (`governance_monitor.py`, the `sensor_eisv` embodied-agent path).

**E_obs** — productive capacity (`_compute_E`):
```
decision_e = exp-weighted (α=0.3) mean of verdict scores over last 10 decisions
             {proceed/approve: 1.0, guide: 0.7, revise/reflect: 0.5, pause/reject: 0.0}
coh_e      = mean coherence remapped [0.35, 0.65] → [0.3, 0.9]
cal_e      = clamp(1 − complexity_divergence, 0.3, 1.0)
E_obs      = 0.40·decision_e + 0.30·coh_e + 0.30·cal_e         # no outcomes
           = 0.35·decision_e + 0.25·coh_e + 0.20·cal_e + 0.20·outcome_e   # with outcomes
```

**I_obs** — claims-match-results (`_compute_I`):
```
cal_I = clamp(1 − calibration_error, 0, 1)     # calibration_error = drift-blended claimed-vs-observed deviation (monitor_drift.py), not literally |confidence − success|
coh_I = split-half coherence trend, mapped to [0.3, 0.9]
I_obs = 0.6·cal_I + 0.4·coh_I                                   # no outcomes
      = 0.50·cal_I + 0.30·coh_I + 0.20·consistency_I            # with outcomes
```

**S_obs** — drift from own normal behavior (`_compute_S`):
```
drift_s  = min(1, drift_norm · 1.5)
regime_s = regime transitions / window      (instability)
cd_s     = min(1, complexity_divergence)
S_obs    = 0.40·drift_s + 0.35·regime_s + 0.25·cd_s
```

Optional small blends (when those signals exist): continuity-log inputs (≤20%), tool error-rate (≤15%), tool velocity / unique-tools ratio (≤10%). The weights are hand-set, not derived — that is the honest status of the current estimator.

## Step 2 — Smoothing, and what V really is (`src/behavioral_state.py`)

State is an EMA of the observations (α ramps from ~0.5 during bootstrap to a configured value):
```
E = (1−α)·E + α·E_obs        I = (1−α)·I + α·I_obs        S = (1−α)·S + α·S_obs
```

**V is not an independent dimension.** It is the EMA-smoothed E−I imbalance (`behavioral_state.py`, `_raw_valence()` fed into the `update()` EMA):
```
raw_v = E − I
V     = (1−α_V)·V + α_V·raw_v
```
So "four-dimensional state vector" is really three observed axes (E, I, S) plus a derived imbalance readout (V). V is surfaced separately because its **sign** is operationally actionable — positive = running hot (energetic but claims outrun results), negative = running careful (coherent but low progress) — not because it carries independent information.

(If you grep the codebase you will find a second `_compute_V` — a slope-plus-level formula — in `behavioral_sensor.py`. It is **unused on the verdict path**: `governance_monitor.py` passes only E/I/S observations to `self._behavioral_state.update()`, which recomputes V as the EMA of E−I above. The live V is the one described here.)

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

- Total risk = sum of named components (`low_E`, `low_I`, high-`S`, `|V|`, …), each with an explicit weight.
- **Warmup** (first ~30 check-ins): fixed universal thresholds.
- **After warmup**: self-relative z-score deviations from the agent's *own* behavioral baseline.
- **Absolute safety floors always apply**, overriding the baseline.
- Self-relative deviation risk is **gated by absolute basin health** (issue #689): inside the healthy basin a deviation from your own norm is treated as information, not danger; the gate opens only as a dimension leaves the basin toward its absolute floor. (This replaced a flat σ-floor that was false-pausing ultra-stable agents.)

Internally the assessment emits a `safe` / `caution` / `high-risk` label; that drives the binary `proceed` / `pause` action (qualified by a sub-action), which the agent reads back as `proceed` / `guide` / `pause` / `reject`.

## Deployed vs. target, at a glance

| Coord | Deployed today (tier: heuristic / resource) | Target semantics (Paper v6) |
|---|---|---|
| **E** | weighted blend of decision-success, coherence, complexity-calibration, outcomes | negative variational free energy (−F) |
| **I** | calibration accuracy + coherence trend (+ outcome consistency) | mutual information I(context; response) |
| **S** | drift-norm + regime instability + complexity divergence | response-distribution entropy H |
| **V** | EMA-smoothed E−I imbalance (derived) | accumulated free-energy residual |

The paper states this plainly: the deployed resource-rate form *"is **not** equivalent to −F and does not approximate it under stationarity in any formal sense."* The target forms become instrumentable only when the inference layer exposes the quantities they require (e.g. token-level logprobs for entropy). Until then, the heuristic is the claim and the information-theoretic form is the direction.

## Don't take this document's word for it

Whether these numbers add useful signal beyond dumb baselines is an **open, measured** question — not an assumption. The [Reviewer Guide's falsifiability harness](REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores EISV/prior-state features against deliberately boring baselines such as `previous_outcome_bad` on ranking (AUC) and calibration (Brier), then self-labels each slice (`INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING`). Treat that as a test of calibration and falsifiability for the proprioceptive signal, not as the headline purpose of UNITARES. The current read is a weak early signal on the task scope at short lead, no demonstrated prevention, and a caveat that the lift may be carried by a single `prior_risk` feature rather than the full decomposition. Run it yourself.
