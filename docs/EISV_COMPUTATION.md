# How EISV Is Actually Computed

**EISV computes a compact, time-smoothed record of runtime observations and controller history.** The behavioral path blends supplied features into E, I and S, smooths them, and derives V from the E/I observation gap. Assessment compares that state with thresholds and, after warmup, an adaptive baseline. Policy uses the assessment alongside other decision inputs.

## Role, runtime and evidence

EISV is a maintained instrument within UNITARES. It supplies a compact history
of runtime observations, baseline comparisons, feedback and inferred policy
recommendations. Its integration is operational; what a reading diagnoses and
whether a consumer benefits from it are separate questions.

| Question | Current contract | Owner |
|---|---|---|
| Does it run? | Successful fresh check-ins run EISV synchronously. There is no shipped no-assessor check-in setting. Keyed replay returns the accepted checkpoint without a new assessment; a later assessment failure can leave that checkpoint accepted. | [Check-in acceptance](operations/checkin-refactor-characterization.md#neutral-checkpoint-acceptance-2026-11-02-increment) |
| Is it becoming optional? | Optional-module separation is an adopted, staged architecture direction. Default `observe` posture controls actuation; it does not disable estimation. Instrument repair and evaluation remain supported while boundaries are separated. | [Boundary proposal](proposals/active/eisv-core-boundary-v0.md), [instrument-quality plan](plans/eisv-instrument-quality-2026-10-13.md) |
| What do the numbers mean? | The formulas measure defined blends of observations and controller history. The names carry intended interpretations; physical and information-theoretic meanings remain research targets. | The computation and producer definitions below |
| Does it help? | Signal response, useful summarization, prediction and intervention benefit require evidence suited to each claim. Operational use or architectural dependency alone establishes none of those benefits; inconclusive outcome evidence does not establish that the signals are uninformative. | [Evidence boundaries](EVIDENCE_AND_LIMITS.md#evaluation-boundaries), [evaluation catalog](EVALUATION_INDEX.md) |
| What may it decide? | A policy recommendation and an applied lifecycle hold are distinct. Agent-facing instructions must name the effective action and actual write gate, with their source and scope. | [Reading the policy action](manual/05-reading-the-signals.md#53-the-policy-action), [interface contract](INTERFACE_CONTRACT.md#policy-recommendation-and-actuation) |

Use this section when summarizing EISV elsewhere. Describe its implemented role
directly, link the evidence for a claimed benefit, and label roadmap work as
staged. Basin names, `healthy` status labels and approval actions are model or
policy classifications, not independent certification of task quality. A
favorable change in a reading is a reason to inspect the underlying evidence,
not sufficient evidence of improvement or recovery by itself.

## What the signals record

These are operational definitions of the current behavioral path (era `SN1`, where `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1` and `UNITARES_PER_AGENT_I_CALIBRATION=1` are enabled by default). The formulas below specify the weights, defaults and optional inputs, as well as the legacy controller/fleet terms restored only when those flags are set to `0`.

| Output | What contributes to it | What a change records |
|---|---|---|
| **E** | Complexity divergence, non-echo outcome success, and available continuity and tool features (prior governance decisions and legacy controller coherence level are excluded by default under `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`) | A change in this weighted blend of supplied divergence, outcomes, and tool/continuity observations (or the `0.85` default when neither base input is supplied) |
| **I** | Eligible per-agent tactical calibration accuracy, non-echo outcome-score consistency, and available continuity and tool-variety features (shared fleet calibration and legacy controller coherence trend are excluded by default; falls back to `0.75` when neither per-agent gate is met) | A change in this per-agent calibration/consistency blend, or the uncalibrated fallback when verification/outcome supply is absent |
| **S** | Drift norm, complexity divergence, and available continuity and tool features (controller regime instability is excluded by default under `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`) | A change in the blend of deviations and activity features (with `UNITARES_SENSOR_DRIFT_AGENT_LOCAL=0`, `drift_norm` still includes fleet `calibration_deviation` and constant `stability_deviation=0.2`) |
| **V** | A separately smoothed difference between raw E and I observations | The direction and persistence of the gap between those two indices |

The observations, their derived summaries, and the resulting policy actions are distinct records. When legacy self-loops are re-enabled (`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`), prior decisions enter E and controller history enters E/I/S so a policy response can change later readings even when supplied task and tool features are unchanged. Under the default `SN1` posture those controller self-loops are excluded from base E, I, and S observations, though the ODE still supplies structural basin, void and coherence checks and legacy drift components when `UNITARES_SENSOR_DRIFT_AGENT_LOCAL=0`. Retain that provenance when explaining a later reading. A derived or correlated signal need not be a new independent observation to be retained and inspected.

This resolves the descriptive question: **the instrument records specified features and their evolution.** Which changes diagnose a particular problem, whether this compression helps a consumer, and which actions should follow are separate evaluation questions. An inconclusive outcome-prediction study does not erase the recorded signal; a responsive signal does not settle those other questions. The [claim ledger](EVIDENCE_AND_LIMITS.md#claim-ledger) holds the evidence for benefit claims.

The historical names describe intended interpretations: productive capacity (E), information integrity (I), entropy (S), and valence (V). The current formulas define weighted indices, not physical energy, mutual information or response-distribution entropy. Those information-theoretic quantities remain research targets in [Paper v6](https://github.com/cirwel/unitares-paper-v6). Reading the implemented indices does not depend on establishing that interpretation.

Provenance identifies the producer: `primary_eisv_source` in the metrics (`behavioral` vs `ode_fallback`), `measurement_source` in the telemetry envelope, and an unconditional `coherence_source`/`coherence_role` stamp naming the legacy ODE producer. Per-coordinate `e_source`/`i_source`/`s_source` tags are attached when the opt-in grounding stage runs (`UNITARES_GROUNDING_SHADOW`/`_APPLY`, both default off — `src/mcp_handlers/updates/enrichments.py`, `run_grounding_stage`). The definitions here describe the behavioral path; inspect those source fields before interpreting a particular reading. Physical sensor inputs can enter the same smoothing stage, so `primary_eisv_source=behavioral` identifies that output path, not the origin of its inputs; inspect input provenance separately.

This document is the owner of the EISV instrument: its inputs, smoothing, risk assessment, margin, and the evidence about what it does and does not show. The [architecture](UNIFIED_ARCHITECTURE.md) places it in the system, and [manual §5.3](manual/05-reading-the-signals.md#53-the-policy-action) owns how an agent handles the policy action it returns.

## Pipeline (primary, verdict-driving path)

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(complexity,    (behavioral_         (behavioral_    (behavioral_          proceed
 outcomes,       sensor.py)           state.py)       assessment.py)        (+guide) /
 drift, tools)                                                               pause
```

The dynamical-systems model (`governance_core/`, the ODE; older docs use a
retired physics-flavoured name for it, which is not a claim about physical
quantities) is
**not the direct verdict owner** by default: behavioral assessment owns the
post-warmup assessment. The final decision also uses ODE state for structural
basin, void and coherence checks (`src/monitor_decision.py`). Under the default
`SN1` posture (`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`), the behavioral sensor
no longer blends legacy ODE `C(V)` (`coh_e`/`coh_I`), `decision_history`, or
`regime_history` into behavioral E/I/S observations; those terms enter only when
`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`. While `UNITARES_SENSOR_DRIFT_AGENT_LOCAL=0`
(default), `drift_norm` still carries ODE `coherence_deviation` and the fleet
tactical `calibration_deviation` scalar.

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

**Timing on the live path.** When a warm, hydrated monitor is already in memory, the handler computes the observation before acquiring the agent lock (`prepare_unlocked_inputs` via `src/behavioral_sensor.py:populate_behavioral_sensor_state`). It reads `drift_norm`, `complexity_divergence` and the continuity E/I/S inputs from what the monitor stored at the previous check-in (`_last_drift_vector`, `_last_continuity_metrics`), so those inputs lag by one check-in, while tool-usage statistics, recent outcomes and the calibration scalar are read at the current check-in. On the first check-in after a server restart (when `mcp_server.monitors` is empty or the monitor has not yet been hydrated / has no prior `_last_drift_vector` or `_last_continuity_metrics`), `prepare_unlocked_inputs` gathers those monitor-independent inputs unlocked into call-local context (`_behavioral_sensor_context`), `assess_eisv` drains `_needs_hydration` and restores `previous_void_active` after hydration, and `governance_monitor.process_update` pairs them with that check-in's freshly computed `continuity_metrics` and `drift_vector` so the first post-restart tick carries full `sensor_eisv_source="behavioral"` provenance and non-empty drift/continuity. Because that first post-restart check-in also stores its `drift_vector` and `continuity_metrics` onto the monitor, its values are read once more when the second check-in resumes the warm one-check-in-lag path, while unpersisted values from the final check-in before the restart are not replayed. Direct `process_update` callers without upstream context continue through the in-monitor fallback (`behavioral_sensor`). On steady-state check-ins a check-in's own complexity report and response structure still first reach E, I and S at the next check-in (contract ledger row 54).

**E_obs** — complexity calibration and non-echo outcomes (`_compute_E_components`; default `SN1` with `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`):
```
cal_e      = clamp(1 − complexity_divergence, 0.3, 1.0)                 # default cd=0.15 => 0.85
outcome_e  = 0.3 + 0.6·(share of ≥3 non-echo outcomes not flagged is_bad)
E_base     = 0.50·cal_e + 0.50·outcome_e                                # both observed
           = 1.00·outcome_e                                             # outcomes only
           = 1.00·cal_e                                                 # complexity only, or 0.85 default when neither
```
*(Legacy `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`: includes `decision_e` — exp-weighted α=0.3 over last 10 decisions `{proceed/approve: 1.0, guide: 0.7, revise/reflect: 0.5, pause/reject and every pause sub-action (risk_/basin_/coherence_/void_pause, cirs_block): 0.0; unrecognized: 0.5}` — and `coh_e`, mean `legacy_tanh_v` feedback remapped `[0.35, 0.65] → [0.3, 0.9]`, with `0.40·decision_e + 0.30·coh_e + 0.30·cal_e` or `0.35/0.25/0.20/0.20` with ≥3 outcomes.)*
(What lands in `decision_history` is the sub-action when one exists, so the live vocabulary is the `monitor_decision.py` sub-action set, not just the coarse names.)

**I_obs** — per-agent calibration accuracy and outcome consistency (`_compute_I_components`; default `SN1` with `UNITARES_PER_AGENT_I_CALIBRATION=1` and `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`):
```
cal_I         = clamp(1 − agent_calibration_error, 0, 1)                # ≥5 recent pairs or pooled with prior_error
consistency_I = max(0.3, 1 − 4·var(non-echo outcome scores))            # ≥3 finite non-echo outcome scores
I_base        = (5/7)·cal_I + (2/7)·consistency_I                       # both eligible
              = 1.00·cal_I   |   1.00·consistency_I                     # one eligible
              = 0.75 (uncalibrated_base_fallback)                       # neither eligible
```
*(Legacy `=0` switches: `UNITARES_PER_AGENT_I_CALIBRATION=0` restores the shared fleet `calibration_error` scalar from `get_mean_calibration_error` → `src/calibration.py` or `drift_vector.calibration_deviation` on the monitor fallback; `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0` restores `coh_I`, split-half trend of `legacy_tanh_v` mapped `0.6 + 3·diff` clamped `[0.3, 0.9]` at `0.40` without outcomes or `0.30` with outcomes.)* Agent-reported `ethical_drift` is telemetry and is never blended into the vector.

The outcome-consistency term records variance: constant success scores and constant failure scores both have zero variance. The derivation marks this term `health_evidence=false`; outcome success enters E separately. Read consistency and success as separate properties. Matching reported confidence to suppressed performance can still resemble honest lower capability in the monitored trace; see the [threat model](SCOPE_AND_THREAT_MODEL.md) and the [concealment boundary fixture](../tests/test_calibrated_capability_concealment.py).

**S_obs** — drift and complexity divergence (`_compute_S_components`; default `SN1` with `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`):
```
drift_s  = min(1, drift_norm · 1.5)                                     # default dn=0.2 => 0.30
cd_s     = min(1, complexity_divergence)                                # default cd=0.1 => 0.10
S_base   = clamp((8/13)·drift_s + (5/13)·cd_s, 0.05, 1)                 # both observed or both defaulted
         = clamp(1.00·drift_s, 0.05, 1)  |  clamp(1.00·cd_s, 0.05, 1)   # single observed term
```
*(Legacy `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`: adds `regime_s = transitions / (window − 1)` over the last 10 regime labels as `clamp(0.40·drift_s + 0.35·regime_s + 0.25·cd_s, 0.05, 1)`.)*

**The drift vector** (`src/monitor_drift.py`, `compute_drift_vector`). Four observable signals define the drift vector whose norm feeds entropy (`drift_norm` above):

| Signal | What it measures |
|--------|-----------------|
| Calibration deviation | Worst populated tactical bin's overconfidence (expected minus observed accuracy, floored at zero) |
| Complexity divergence | Self-reported complexity vs system estimate |
| Coherence deviation | How far coherence has moved from baseline |
| Stability deviation | Decision-pattern instability (`1 − decision_consistency`) |

No human oracle is needed for runtime drift estimation. Independent exogenous outcomes still matter for calibration and research validation.

**The behavioral sensor reads an agent-local drift norm** (`UNITARES_SENSOR_DRIFT_AGENT_LOCAL`, default `0`, which passes the full vector norm; set to `1`/`true`/`yes`/`on` to read the agent-local norm). It ships off so that a routine deploy does not start a new instrument era. Turning it on is a separate operator decision, and that change sets the default, adds era `SD1` to `config/eisv_instrument_eras.json` and regenerates the golden trajectories. The reason for it: on the sensor path two of the four components above carry no per-agent information. Stability deviation is 0.2 for every agent, because the drift engine is called with no decision and `decision_consistency` never leaves its 0.8 default. Calibration deviation is the server-wide tactical worst bin, one number for the fleet that moves only when a hard-exogenous outcome arrives. Coherence deviation tracks legacy ODE control feedback, the self-loop class `SN1` excludes from E and I. The sensor's `drift_norm` is therefore the norm of the agent's supplied complexity divergence and its own tactical calibration error (at least 10 verified pairs, freshness `recent`), and `None` when neither exists, so the drift term in S is reported unobserved rather than as a reading. `derivation.drift_input` records the full norm, the components used, and why each other component was left out. The drift vector itself, and the ODE forcing it feeds, are unchanged.

Missing inputs fall back to fixed neutral defaults rather than dropping the term (`cal_I` 0.75, `drift_norm` 0.2, complexity divergence 0.15 in E / 0.1 in S, both `coh` terms 0.6), so a signal outage reads as "unremarkable", not as zero.

**Unread axes are held, not filled** (`UNITARES_HOLD_UNREAD_AXES`, default `0`; set to `1`/`true`/`yes`/`on` to hold). It ships off so that a routine deploy does not start a new instrument era. Turning it on is a separate operator decision, and that change sets the default, adds era `HU1` to `config/eisv_instrument_eras.json` and regenerates the golden trajectories. With the flag on, the fill above still produces the arithmetic the component record and the shadow ablations report, and it is still the legacy ODE's anchor. The behavioral state no longer consumes it for an axis that had no supplied base term:
- The sensor reports no reading for that axis, and the optional blends on it are discarded for the check-in (they adjust a reading, and there is none).
- The smoothed value keeps its last reading, takes no baseline sample and no trend entry. `V` steps only when `E` and `I` were both read on the same check-in.
- The first reading on an axis seeds its value and restarts its baseline. State restored from before `HU1` starts with every axis unread: it keeps the numbers it had, as placeholders, until a reading replaces them.
- Switching the flag off does not reset a state written under it. Every check-in is a reading again, but an axis it never read is still seeded by its first reading and scored on fixed thresholds until it has a baseline. `behavioral.state.written_under_hold` records which rule the reading counters were kept under, separately from `hold_unread` (the rule the state updates with now), so that holds across later restarts and hand rebuilds.
- The assessment scores an axis only while it has a reading from the last 10 check-ins (`STALE_AFTER_UPDATES`, a chosen bound equal to the bootstrap length). An unscored axis contributes no risk, its absolute floor does not fire, and it is listed in `behavioral.assessment.unscored_axes`; a `safe` verdict says nothing about it. `behavioral.state` carries `readings`, `since_reading`, `unread` and `unscored` per axis.
- Inside `behavioral.state`, the smoothed `E`/`I`/`S`/`V` fields stay numbers: the last reading, or the bootstrap value (0.5, 0.5, 0.2, 0.0) for an axis never read. Read them with `unread`. However, top-level `metrics.E`/`I`/`S`/`V` (`primary_eisv`, via `UNITARESMonitor.get_primary_eisv`) reports `behavioral.state` only once `behavioral.confidence >= 0.3`; before 3 check-ins read something or when every axis goes stale (`confidence == 0.0`), `primary_eisv_source` reports `ode_fallback` and top-level `metrics.E`/`I`/`S`/`V` returns the ODE state (which continues receiving the filled `sensor_eisv` arithmetic as its spring anchor when coupling allows).
- Before three check-ins of history the sensor cannot run; under the hold those check-ins read nothing, where the response-structure inputs used to stand in.

Optional blends (when those signals exist): continuity-log inputs (≤20%), tool error-rate (≤15%), tool velocity / unique-tools ratio (≤10%). The weights are hand-set engineering parameters.

**De-looping and per-agent calibration switches** (both **default ON** (`1`, era `SN1`), hashed into the instrument configuration in `src/eisv_instrument.py`; set to `0`/`false`/`no`/`off` to restore the legacy self-loop and shared-fleet-calibration formulas above):
- `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS` (default `1`): excludes the endogenous controller self-loop components from the base formulas (`decision_history` and `legacy_coherence_level` in `E`, `legacy_coherence_trend` in `I`, `regime_instability` in `S`), filters out server-generated `trajectory_validated` and `cirs_resonance` outcome echoes, and **renormalizes the remaining observed base terms to sum to `1.0`** before the optional post-base blends (`E_base = 0.50·cal_e + 0.50·outcome_e` when both are observed or `1.00` on whichever single term is observed; `I_base = (5/7)·cal_I + (2/7)·consistency_I` when both are observed, `1.00` on whichever single term is observed, or `0.75` `uncalibrated_base_fallback` when neither is observed; `S_base = clamp((8/13)·drift_s + (5/13)·cd_s, 0.05, 1)` when both are observed or `1.00` on whichever single term is observed; if no non-loop base input is observed on `E` or `S`, its documented default fallback carries the base). By contrast, the one-step measurement-only shadow ablations (`compute_legacy_coherence_dependency_shadow`, `compute_decision_self_loop_shadow`, `compute_regime_self_loop_shadow`) keep the legacy weights when `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0` and substitute constant fleet/transfer-midpoint stand-ins so their `candidate_minus_deployed` deltas measure single-step level attribution rather than reweighting.
- `UNITARES_PER_AGENT_I_CALIBRATION` (default `1`): replaces the shared fleet `calibration_error` scalar in `I` with the agent's own verified confidence–outcome pairs (`agent_calibration_pooled` when a reference-cohort `prior_error` is supplied, or `agent_tactical_calibration` when `n_verified_pairs >= 5` and `freshness_status == "recent"`), falling back to calibration-free `I` (`0.60·coh_I + 0.40·consistency_I` when `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`; `1.00·consistency_I` with ≥3 non-echo outcomes or an explicit `0.75` `uncalibrated_base_fallback` without outcomes when `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`). Under `SN1`, `1.00·consistency_I` when calibration is ineligible is accepted by design: `consistency_I` (`health_evidence=false`) measures score variance only, while outcome success/failure direction enters `E_base` at `0.50` weight, so constant failures lower `E_obs` and pull `V` (`E_obs - I_obs`) negative rather than conflating failure with dispersion.

**Signal-hygiene and clean actor work-tool switches** (all **default OFF** (`0`), hashed individually into `src/eisv_instrument.py`):
- `UNITARES_DROP_CONTINUITY_E_BLEND` (default `0`): unconditionally omits the `0.20` `continuity_E_input` post-base blend from behavioral `E` (avoiding both chronic `0.30` floor clipping and a clip-threshold cliff) while keeping `ContinuityMetrics` and derivation diagnostics populated.
- `UNITARES_DROP_UNIQUE_TOOLS_I_BLEND` (default `0`): unconditionally omits the `0.10` `unique_tools_ratio` post-base blend from behavioral `I` (removing call-volume confounding and specialist penalties) while keeping raw tool-diversity diagnostics populated.
- `UNITARES_ACTOR_WORK_TOOL_STATS` (default `0`): reads an actor's 1-hour work-tool stats (`tool_error_rate`, `tool_call_velocity`, `unique_tools_ratio` diagnostics) from indexed PostgreSQL `audit.tool_usage` (`src/audit_db.py:get_behavioral_work_tool_stats_async`) excluding `lease.*`, removed tools, meta/governance-lifecycle tools (`BEHAVIORAL_EXCLUDED_META_TOOL_SET` and its aliases), and server/EISV-caused error types (`BEHAVIORAL_NON_AGENT_ERROR_TYPES`), caching them on `agent_state["_actor_work_tool_stats"]` so continuity grounding (`governance_monitor`), anomaly entropy (`phases`), Welford baselines (`post_update_helpers`), and fallback confidence derivation (`confidence`) share the clean actor stats with zero fallback to the REST/stdio JSONL tracker.

`coh_e` and `coh_I` (active only when `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`) are especially important to interpret honestly. They were
introduced to reduce E saturation and differentiate agents while `coherence`
still meant `C(V_ODE)`. The later behavioral-EISV migration promoted E/I/S/V
but left the coherence producer behind, so when legacy self-loops are enabled
these terms carry a demoted ODE controller signal into the behavioral vector.
The derivation tags that input; the legacy weights apply only with `=0`.

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
- **With unread axes held (`UNITARES_HOLD_UNREAD_AXES=1`)** these stages count check-ins that
  read something, not all check-ins. The sensor needs three decisions of
  history, so a new agent's first three check-ins read nothing: the Φ prior
  owns check-ins 1–2 as it does with the flag off, no verdict is drawn on 3–5
  (next item), fixed thresholds apply on 6–27, and self-relative scoring
  starts at check-in 28, later still if more check-ins read nothing. An axis
  with fewer than 5 baseline samples stays on the fixed thresholds.
- **Insufficient reading draws no verdict (hold only).** The Φ prior stands in
  for an assessment that is on its way. An agent that keeps checking in
  without supplying sensor inputs never reaches behavioral authority, and for
  it the prior would decide indefinitely. So once the agent is past cold start
  by check-in count (the 0.3 authority threshold applied to check-ins, which
  is check-in 3) and the behavioral state still has less than 0.3 confidence,
  no verdict is drawn: the published verdict is `safe` with risk 0, the driver
  is `insufficient_reading`, the Φ-derived fast trip does not apply, and the
  verdict is graded provisional. `safe` there means nothing was measured. The
  same holds for an agent whose readings have all gone stale. The independent
  verification floor still applies, and structural pauses are unchanged. With
  `UNITARES_PHI_TELEMETRY_ONLY=0` the rule is off, because Φ is then a
  configured floor and not a placeholder.
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
  with lag. Issue #1995 closed under Option 1 on 2026-10-23 keeping floors as
  component bounds after the 2026-09-13 wake read (zero baselined E/I/S
  breach-and-safe rows in the 11-day soak window; all 59 baselined rows were
  boundary |V| jitter on one agent where an un-hysteretic floor would induce
  false CIRS resonance).
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

For `continuity_fallback`, `derivation.inputs.continuity` records the inputs
where continuity E/I/S were computed: approximate report-token count,
wall-clock check-in gap, structural counts, self-complexity, formula branch,
raw/pre-clip values and bounds. It includes no report text or tool names.
`derivation.selection` records the values actually handed to the behavioral
filter, including missing-input and template-exclusion defaults. Missing traces
remain unknown. A clip at a bound and an actual change by clipping are separate
fields; neither means that an input was defaulted. A zero report-token count
does not establish zero work. Check-in spacing is not generation duration.
When the gap is missing or nonpositive, E uses a different report-length
formula. Its dependency is `fallback_formula`, with the reason recorded
separately from a scalar missing-input default. This remains visible even for
zero report tokens and zero V; neither clipping nor signed cancellation
turns it into an observed rate.

The trace captures complexity/divergence **before** later tool-use blending
and rate-of-change adjustments. Those adjustments currently do not recompute
continuity E/I/S, so the final public divergence can differ from the value that
produced I/S. The record preserves that distinction rather than retroactively
substituting the final divergence. `default_dependent` labels the whole input
as depending on a default; it does not quantify a fractional contribution.

To inspect inherited dependencies, export one identity's ordered, unsampled
`eisv.telemetry.v1` envelopes into
`{"agent_id": "...", "records": [ ... ]}` and run:

```sh
python3 scripts/analysis/eisv_provenance_replay.py trace.json --out replay.json
```

This offline tool checks adjacent version-2 behavioral updates with the real
`BehavioralEISV` implementation and four-decimal serialization bounds. The
first saved state has unknown ancestry. Gaps, resets, unsupported metadata and
numeric mismatches re-anchor that history as unknown. Duplicate measurement
IDs are rejected; repeated content alone is not a duplicate. V retains
separate E-input and I-input ancestry with one shared unknown anchor, so signed
cancellation cannot erase a default or unknown dependency. Only reconciled
convex component recipes receive fractional allocations; additive or nonlinear
recipes remain unresolved rather than being renormalized. Source labels and
coefficient totals are not confidence, independent evidence, readiness rules,
or proof of target validity. Baseline provenance and ODE prediction/innovation
are outside this replay; no live estimator, baseline, policy or flag is changed.
`observed_*` means present at the recorded computation boundary. Upstream
values can themselves contain defaults (including schema-defaulted
self-complexity); this ledger does not establish recursive, default-free
ancestry for those values.

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
