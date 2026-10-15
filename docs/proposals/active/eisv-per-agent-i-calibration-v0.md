# Per-Agent EISV `I` Calibration Term and Cross-Channel Homogenization Audit (v0)

**Status:** Active design note and offline comparison (`#2818`), 2026-10-15.
Documentation and offline analysis only. This document changes no live
predictor, weight, threshold, database write, or calibration bin feeder.
**Preservation gate:** The deployed `I` estimator and legacy fleet calibration
bins stay fixed through the registered `2026-12-01` outcome-grounding stop-rule
read ([`eisv-outcome-grounding-stop-rule-v0.md`](../registered/eisv-outcome-grounding-stop-rule-v0.md),
`#1425`) and operator-declared report finality
([`eisv-core-boundary-v0.md`](eisv-core-boundary-v0.md),
[`eisv-instrument-quality-2026-10-13.md`](../../plans/eisv-instrument-quality-2026-10-13.md)).
Do not re-open supply into the fleet bins meanwhile.
**Out of scope:** Outcome-prediction claims (`#1425`) and proper-scoring
incentive design ([`forecast-bound-calibration-mirror-v0.md`](forecast-bound-calibration-mirror-v0.md),
`#2605`).

---

## 1. Problem Statement

The EISV proprioception contract
([`docs/ontology/eisv-proprioception-contract.md`](../../ontology/eisv-proprioception-contract.md)
§2, §3.2) defines behavioral `I` (Information Integrity) as a **per-agent**
proprioceptive state channel measuring how well *this* agent's claims,
reasoning, and actions hold together.

In the deployed behavioral sensor ([`src/behavioral_sensor.py:822-909`](../../../src/behavioral_sensor.py)),
the dominant component of `I` is `cal_I = 1.0 - calibration_error` (`0.50` base
weight when `>= 3` recent outcomes carry a finite score, `0.60` otherwise;
`0.360` or `0.432` effective weight after the `0.20` `continuity_I_input` and
`0.10` `unique_tools_ratio` blends at `:179-200`). On the live check-in path,
`calibration_error` is supplied by `get_mean_calibration_error()`
([`src/mcp_handlers/updates/context.py:99-157`](../../../src/mcp_handlers/updates/context.py)),
which takes no `agent_id` and averages `calibration_checker.compute_calibration_metrics()`
over fleet-wide strategic bins with `count >= 5`.

Two distinct defects compound on this path:

1. **Structural homogenization (design-intent defect, `#2818`):** Even when the
   fleet strategic bins were actively receiving samples (prior to `2026-08-02`),
   `get_mean_calibration_error()` returned a single fleet-wide scalar on every
   check-in. Injecting a fleet scalar at `36%–43%` effective weight into a
   per-agent channel compresses between-agent variance by construction.
2. **Frozen upstream supply (ledger rows 15, 48):** After `#1445` (`2026-08-02`)
   excluded server-scraped confidence (`prev_confidence_fallback` and
   `audit_trail_fallback`) from training calibration, the fleet strategic bins
   froze at `282` samples (`calibration_error = 0.147446577`, `cal_I = 0.852553423`).
    across all `18,021` non-synthetic `I` observations (`318` identities) in the
   30-day window ending `2026-10-12`, `cal_I` had **zero** within-agent and
   **zero** between-agent variance.

---

## 2. Audit of Per-Agent Channels for Fleet-Scoped or Frozen Inputs

Criterion 1 of `#2818` requires auditing all per-agent channels beyond
`coherence` (`C(V_ODE)`, pinned near `0.49` with between-agent SD `0.0032`
after `69ee5a799` demoted ODE `V`; ledger row 1) for fleet-scoped or frozen
inputs. Table 1 lists every per-agent channel carrying a fleet-scoped,
frozen, or quasi-frozen input on `master`.

| Per-agent channel | Input / component | Code path | Scope & runtime status | Per-agent homogenization impact |
|---|---|---|---|---|
| **Behavioral `I`** (`raw_I`, `state.I`) | `cal_I = 1.0 - calibration_error` (`0.50` / `0.60` base weight; `0.360` / `0.432` effective) | `src/behavioral_sensor.py:839-844`; fed by `get_mean_calibration_error()` in `src/mcp_handlers/updates/context.py:99-157` (fallback in `src/governance_monitor.py:1295-1311` uses `drift_vector.calibration_deviation`) | **Fleet-scoped + frozen** since `2026-08-02` at `calibration_error = 0.1474` (`cal_I = 0.8526`, 282 strategic samples; ledger rows 2, 15, 48) | Injects a constant `+0.3069` (with outcomes) to `+0.3683` (without outcomes) into every agent's raw `I` observation; between-agent SD of `cal_I` is `0.0000`. |
| **Behavioral `I`** (`raw_I`, `state.I`) | `coh_I = _coherence_trend(coherence_history)` (`0.30` / `0.40` base weight; `0.216` / `0.288` effective) | `src/behavioral_sensor.py:846-848, 916-940`; fed by `monitor.state.coherence_history` (`C(V_ODE)`) in `src/mcp_handlers/updates/phases.py:944` | **Quasi-frozen** (`C(V_ODE)` restricted to `[0.455, 0.499]`; ledger row 1) | Split-half trend sits near the `0.60` neutral midpoint (`0.50 + diff * 5.0`), contributing `~0.13–0.17` with negligible per-agent spread. Together with `cal_I`, `57.6%–72.0%` of raw `I` weight is frozen or quasi-frozen. |
| **Ethical drift `Δη`** (`drift_vector`) | Pillar 1: `calibration_deviation` | `src/monitor_drift.py:19-46, 113-117` (`compute_calibration_error`) $\to$ `governance_core/ethical_drift.py:356-367` | **Fleet-scoped + frozen** since `2026-08-02` at `0.2414` (worst fleet tactical bin `0.9–1.0`, 277 samples; ledger row 15b) | Because `compute_calibration_error()` returns a non-`None` fleet scalar on every check-in, the per-agent fallback in `ethical_drift.py:362-366` (`\|current_confidence - baseline.baseline_confidence\|`) is **permanently bypassed**. Every warmed agent gets `0.2414` (`×0.3` on `"exploration"`/`"introspection"`, floored by `0.3 * min(0.5, state_velocity)`). |
| **Ethical drift `Δη`** (`drift_vector`) | Pillar 4: `stability_deviation` | `src/monitor_drift.py:116` (`decision=None`) $\to$ `governance_core/ethical_drift.py:345-350, 412-415` | **Agent-keyed but frozen** at `0.20` for all post-warmup (`update_count >= 5`) agents (`1,037/1,037` DB rows have `recent_decisions = []`; ledger rows 21, 37) | Hardcoding `decision=None` leaves `decision_consistency = 0.8` forever, so `stability_deviation = 1.0 - 0.8 = 0.20` for every warmed agent. |
| **Ethical drift `Δη`** (`drift_vector`) | Pillar 3: `coherence_deviation` | `src/monitor_drift.py:72-86` (`control_feedback(V_ODE)`) $\to$ `governance_core/ethical_drift.py:390-405` | **Quasi-frozen** baseline gap (`C(V_ODE)` pinned near `0.49`), dominated by `0.5 * min(0.5, state_velocity)` floor | Eliminates genuine coherence-drop signal in `Δη`; only the `state_velocity` floor moves. |
| **Behavioral `S`** (`raw_S`, `state.S`) | `drift_s = min(1.0, drift_norm * 1.5)` (`0.40` base weight; `~0.32` effective) | `src/behavioral_sensor.py:953-958`; fed by `monitor._last_drift_vector.norm` (`src/mcp_handlers/updates/phases.py:946-948`) | **Propagated fleet + frozen floor** from `Δη` pillars 1 and 4 | For any warmed non-exploratory agent, $\lVert\Delta\eta\rVert \ge \sqrt{0.2414^2 + 0.20^2} = 0.3135$, pinning `drift_s >= 0.4702` (`>= 0.1881` raw `S` before blends). |
| **ODE `S`** (`monitor.state.S`, diagnostic) | `noise_S = calibration_penalty` and resident `s_setpoint` | `src/governance_monitor.py:742-769` $\to$ `governance_core/dynamics.py:156-162`; `config/governance_config.py:976-985` | **Fleet-scoped + frozen** `calibration_penalty = 0.0483` (fleet strategic bins `count >= 2`); **config-frozen** `s_setpoint = 0.0` when `UNITARES_CLASS_CALIBRATION` is unset (ledger rows 15a, 23) | Injects identical entropy drift (`0.0483`) into every agent's ODE `dS/dt` and pulls named residents toward `S* = 0.0`. |
| **Behavioral `E`** (`raw_E`, `state.E`) | `coh_e` (`legacy_coherence_level`, `0.25`/`0.30` base weight; `0.17` effective) and `continuity_E_input` (`0.20` blend; `0.17` effective) | `src/behavioral_sensor.py:706-709, 807-820, 178`; `src/dual_log/continuity.py:212-218` | **Quasi-frozen** `coh_e` (`0.578 ± 0.006`, ledger row 20) and **clip-pinned** `continuity_E_input = 0.30` (divides `response_tokens` by inter-check-in wall-clock idle gap) | Roughly `34%` of raw `E`'s effective weight (`0.17` `coh_e` + `0.17` `continuity_E_input`) sits at near-constant values across agents (ledger rows 20, 47). |
| **Omitted-confidence fallback** | `derive_confidence(state)` | `src/confidence.py:197-238` | **Quasi-frozen + fleet-coupled** (`0.55 * state.coherence + 0.35 * state.I + 0.10 * (1 - state.S) - 0.20 * S`) | Narrowly pins server-derived confidence when callers omit `confidence`; also polluted `audit.events.confidence` before `#1445`. |
| **Self-relative z-score denominator** | `BehavioralEISV.deviation(component)` | `src/behavioral_state.py:65-112, 325-335` (`MIN_MEANINGFUL_EISV_STD = 0.05`) | **Shared constant floor** (`0.05`) triggered by upstream homogenization + EMA smoothing (`alpha_I = 0.08`) | Binds on `69/72` (`95.8%`) baselined agents on `I`, `33/72` on `S`, `8/72` on `E`, `9/72` on `V` (ledger row 19), replacing individual Welford `std` with a shared `0.05` denominator. |
| **Other constant per-agent surfaces** | `metrics['stability']`, `AdaptiveGovernor`, `trajectory_identity` | `src/mcp_handlers/support/research.py:22-40`; `governance_core/adaptive_governor.py`; `src/trajectory_identity.py` | **Constant / saturated** across agents (ledger rows 6–9, 17, 18) | `alpha_estimate = 0.101128` for all agents; `AdaptiveGovernor` saturates at `tau = 0.2519, beta = 0.799` (`445/445`); `stability_score` p50 `0.9996` (`92.8% >= 0.99`). |

---

## 3. Design of the Per-Agent `I` Calibration Replacement

### 3.1 Pooling Rule: Replace the Fleet Term via Empirical-Bayes Partial Pooling

**Rule 1 (Replace, do not blend):** `cal_I(a, t)` replaces `1.0 - calibration_error`
at the existing `calibration_accuracy` component slot (`0.50` base weight with
outcomes, `0.60` without) in `_compute_I_components`
([`src/behavioral_sensor.py:839-844`](../../../src/behavioral_sensor.py)).
A per-agent term must **not** be blended alongside the fleet term: any fixed
weight $\lambda \in (0, 1)$ on a fleet scalar permanently caps the agent's own
calibration authority at $1 - \lambda$ and preserves cross-agent variance
compression even as the agent's verified sample count $n_a \to \infty$.

**Rule 2 (Partial pooling over sample-weighted binned calibration error):**
Let $\mathcal{P}_a(t) = \{(t_i, c_i, y_i)\}_{i=1}^{n_a(t)}$ be identity $a$'s
admissible verified forecast-outcome pairs (Section 3.3) with $t_i < t$.
Partition $\mathcal{P}_a(t)$ across the five confidence bins
$B_1=[0.0,0.5), B_2=[0.5,0.7), B_3=[0.7,0.8), B_4=[0.8,0.9), B_5=[0.9,1.0]$
with bin counts $n_{a,b}(t)$ ($\sum_{b=1}^5 n_{a,b}(t) = n_a(t)$).

When $n_a(t) \ge 1$, define the agent's unpooled sample-weighted expected
calibration error (without a per-bin `count >= 5` step cliff):

$$e_{\text{agent}}(a, t) = \sum_{b:\, n_{a,b}(t) \ge 1} \frac{n_{a,b}(t)}{n_a(t)} \left| \bar{y}_{a,b}(t) - \bar{c}_{a,b}(t) \right| \in [0, 1]$$

where $\bar{y}_{a,b}(t)$ and $\bar{c}_{a,b}(t)$ are the mean verified outcome
score and mean stated confidence in bin $b$. Shrink $e_{\text{agent}}(a, t)$
toward a prior error $e_0 \in (0, 1)$ with pseudocount $k > 0$ (candidate
default $k = 10$):

$$w(n_a) = \frac{n_a}{n_a + k}, \qquad e_{\text{pool}}(a, t) = \bigl(1 - w(n_a)\bigr)\, e_0 + w(n_a)\, e_{\text{agent}}(a, t)$$

$$\text{cal\_I}(a, t) = \operatorname{clip}\bigl(1.0 - e_{\text{pool}}(a, t),\, 0.0,\, 1.0\bigr)$$

**Why sample-weighted binned ECE + shrinkage rather than `#2194` or pairwise MAE:**
- **Eliminates the `#2194` `MIN_PER_BIN = 5` cliff:** The `#2194` candidate
  (`CalibrationChecker.compute_agent_calibration_candidate`,
  [`src/calibration.py:274-339`](../../../src/calibration.py)) requires at
  least 5 pairs *inside a single confidence bin* with newest pair $\le 7\text{ d}$
  old. Across `18,021` check-ins (`318` identities) in `snapshot.jsonl`, that
  step threshold yields **`0` eligible check-ins (`0.0%`)**, and even without a
  staleness cutoff it discards `19/53` (`35.8%`) of identities that have
  `externally_verified` pairs. Partial pooling uses the first verified pair
  immediately with weight $w(1) = \frac{1}{1+k}$ and converges smoothly to
  $e_{\text{agent}}(a, t)$ as $n_a \to \infty$ ($w(n_a) \to 1$).
- **Avoids pairwise MAE's improper penalty on honest intermediate forecasts:**
  For binary outcomes $y_i \in \{0, 1\}$, pairwise MAE $\frac{1}{n_a}\sum |y_i - c_i|$
  has expectation $2c(1-c) > 0$ for a perfectly calibrated forecaster (e.g., an
  agent stating $c = 0.70$ and succeeding $70\%$ of the time gets MAE $= 0.42$,
  whereas stating $c = 1.00$ gets MAE $= 0.30$). Sample-weighted binned ECE
  $e_{\text{ece}}(a)$ converges to $0$ as bin counts grow for any calibrated
  forecaster while still reducing to $|y_1 - c_1|$ damped by $\frac{1}{1+k}$ at
  $n_a = 1$. (Pairwise Brier score $e_{\text{brier}}(a) = \frac{1}{n_a}\sum (y_i - c_i)^2$
  is reported alongside ECE in the offline comparison below.)

### 3.2 Behavior When an Agent Has No Evidence ($n_a = 0$)

When $n_a(t) = 0$:
1. $w(0) = 0$, so $e_{\text{pool}}(a, t) = e_0$ and $\text{cal\_I}(a, t) = 1.0 - e_0$.
2. The component derivation trace records explicit prior-only provenance rather
   than masquerading as observed calibration:
   - `source = "agent_calibration_prior"` (vs `"agent_calibration_pooled"` when $n_a \ge 1$),
   - `observed = False`,
   - `default_reason = "no_verified_agent_pairs"`,
   - `evidence_status = "prior_only"`, `n_verified_pairs = 0`, `pooling_weight = 0.0`.
3. **Choice of prior $e_0$ and threshold coupling (`BASIN_I_HEALTHY = 0.70`):**
   - `_compute_I_components` ([`src/behavioral_sensor.py:842`](../../../src/behavioral_sensor.py))
     already defines `0.75` (`e_0 = 0.25`) as the neutral midpoint when
     `calibration_error is None`.
   - However, `BASIN_I_HEALTHY = 0.70` ([`config/governance_config.py:725`](../../../config/governance_config.py))
     was tuned while the frozen fleet scalar `cal_I = 0.8526` (`e_0 = 0.1474`)
     was injecting `+0.3069` (`0.36 * 0.8526`) into every check-in. As shown in
     Section 4.3, shifting no-evidence agents from `0.8526` to `0.7500` lowers
     per-identity mean `I` across `snapshot.jsonl` (`n = 318`) by `-0.0387`
     (from `mean = 0.7012, p50 = 0.7062` to `mean = 0.6625, p50 = 0.6689`) and
     drops the share of identities with mean `I >= 0.70` from `54.7%` (`174/318`)
     to `19.5%` (`62/318`).
   - Therefore, any future live cutover must **either** (a) set $e_0$ to the
     verified-pair population ECE prior ($\bar{e}_{\text{ece,pop}} \approx 0.1621 \implies \text{cal\_I}_0 \approx 0.8379$,
     computed strictly over admissible verified pairs, never from legacy fleet
     bins) **or** (b) re-center `BASIN_I_HEALTHY` by the exact prior delta
     `eff_w * (0.75 - 0.8526) ≈ -0.037` in the same gated change.

### 3.3 Verified-Outcome and Pre-Committed Forecast Requirements

A pair $(c_i, y_i)$ is admissible into $\mathcal{P}_a(t)$ if and only if **both**
sides of the pair satisfy independent provenance checks:

1. **Pre-committed agent forecast ($c_i$):**
   - Must come from either:
     - a prediction registered before execution (`prediction_source == "registry"`
       with server-recorded mint timestamp $t_{\text{mint}} < t_{\text{outcome}}$), or
     - the same identity's most recent check-in explicit self-report
       (`eisv_telemetry.self_report.confidence_reported`, persisted on
       `core.agent_state` since `#2741`, commit `ba1a90438`) within a bounded
       causal window ($0 < t_{\text{outcome}} - t_{\text{checkin}} \le 24\text{ h}$).
   - **Exclusions:**
     - Post-hoc confidence passed on `record_result` at outcome time
       (`prediction_source == "argument"`, `argument_fallback`, or
       `missing_prediction` / `ttl_expired_fallback` with an argument; Finding F1
       in [`forecast-bound-calibration-mirror-v0.md`](forecast-bound-calibration-mirror-v0.md)),
     - Server-derived confidence (`derive_confidence` in `src/confidence.py:197-238`)
       leaking via `prev_confidence_fallback` or `audit_trail_fallback`
       (`src/db/mixins/audit.py:263-306`),
     - Clamped identity-assurance values (`phases.py:849-853`) where the post-clamp
       number is not the agent's stated claim (`#2603`).

2. **Server-established outcome truth ($y_i$):**
   - Must carry `corroboration_grade == "externally_verified"`
     (`evidence_weight >= 0.85`, produced by `SERVER_SET_TOOL_TRIGGERS` or a
     server-verified execution receipt outside the agent's MCP tool call) and
     `not calibration_excluded` (excluding `shadow_write` and validation fixtures).
   - **Why `TOOL_OBSERVED` (`0.65`) and registry binding alone are insufficient:**
     Public `record_result` calls are capped at `TOOL_OBSERVED` (`0.65`,
     [`src/outcome_corroboration.py:408-430`](../../../src/outcome_corroboration.py)),
     which checks only that the caller-supplied `detail` dictionary contains
     structured keys (`exit_code`, `command`, `files`, `tests`) — it grades
     **caller-described payload shape, not execution truth**. In the preserved
     2026-10-12 corpus (Section 4.1), `tool_observed` pairs (`n = 218`) exhibit
     less than one-third the error (`mean |y - c| = 0.0922, p50 = 0.0400`) of
     `externally_verified` pairs (`n = 444, mean |y - c| = 0.2881, p50 = 0.2000`)
     because the same agent chooses both the confidence and the outcome report.
   - **Host trust-boundary caveat:** As documented in
     [`forecast-bound-calibration-mirror-v0.md`](forecast-bound-calibration-mirror-v0.md)
     §2 (C2) and §7 (F5), `external_signal` rows posted by the local harness hook
     (`outcome-tracker.sh` $\to$ `POST /v1/harness/outcome`) share an OS user
     with local agents and withhold induced test failures. They are exogenous to
     the MCP self-report path (suitable for proprioceptive estimation under a
     non-adversarial local harness), not an adversarial-proof oracle.

3. **Decoupling from the shared 3-consumer gate in `outcome_events.py:643-702`:**
   Today, a single `if` block in
   [`src/mcp_handlers/observability/outcome_events.py:643-702`](../../../src/mcp_handlers/observability/outcome_events.py)
   gates three consumers at `evidence_weight >= 0.65 and not calibration_excluded`:
   - `calibration_checker.record_prediction` (fleet strategic `bins` + `bins_by_agent`),
   - `calibration_checker.record_tactical_decision` (fleet tactical `tactical_bins`, read by `monitor_drift.py`),
   - `sequential_calibration_tracker.record_exogenous_tactical_outcome`.
   The per-agent `I` calibration estimator must read its own verified-pair store
   (keyed on `identity_id` and gated on `externally_verified` + pre-committed
   forecast) rather than changing `_MIN_TACTICAL_EVIDENCE_WEIGHT` or re-opening
   supply into the legacy fleet strategic/tactical bins.

---

## 4. Offline Comparison of Between-Agent Spread and Level on Preserved Data

Criterion 3 of `#2818` requires specifying and running an offline comparison of
between-agent spread and level under the current and proposed terms on preserved
data before any live change.

The comparison is implemented in
[`scripts/analysis/eisv_i_calibration_shadow_read.py`](../../../scripts/analysis/eisv_i_calibration_shadow_read.py)
(unit-tested in [`tests/test_eisv_i_calibration_shadow_read.py`](../../../tests/test_eisv_i_calibration_shadow_read.py))
and run against the frozen, SHA-256-verified `2026-10-12` evaluation dataset in
`~/.unitares/analysis/ei-referent-2026-10-12/`:
- `snapshot.jsonl`: `19,535` rows (`18,021` non-synthetic rows with `I` components
  across `318` identities, `2026-09-14` to `2026-10-12`, sha256
  `7c8d219746a284814cbf54f77bb7b3e4156c708208bff5a9f2d89944a612bbb9`),
- `verified_outcomes.jsonl`: `7,061` rows (`6,552` `externally_verified`, `509`
  `tool_observed`, sha256
  `414bd3fa060278d23411256428bd90ad492983e722e13b5808ca42784e6a3c32`),
- `confidence_obs.jsonl`: `8,449` rows (sha256
  `9995dd7834adfb11cc0aff467173a49dc159ef708581d69e2d09c42a5c0c72b4`).

### 4.1 Grade Stratification of Paired Outcomes (`externally_verified` vs `tool_observed`)

Pairing each outcome in `verified_outcomes.jsonl` with the same identity's most
recent stated confidence within the preceding `24 h` yields `662` pairs across
`113` identities (`n = 113, mean = 5.8584, p10 = 1.0000, p50 = 4.0000, p90 = 12.0000`
pairs per identity). Stratifying by `corroboration_grade` confirms why
`tool_observed` (`0.65`) must be excluded:

| Outcome grade | Paired rows (`n`) | Identities | Pairwise $\|y - c\|$ mean | Pairwise $\|y - c\|$ p10 | Pairwise $\|y - c\|$ p50 | Pairwise $\|y - c\|$ p90 |
|---|---:|---:|---:|---:|---:|---:|
| `externally_verified` (admissible) | `444` | `53` | `0.2881` | `0.1000` | `0.2000` | `0.7000` |
| `tool_observed` (`0.65` shape-graded) | `218` | `60` | `0.0922` | `0.0100` | `0.0400` | `0.1800` |

### 4.2 Per-Identity Calibration Component (`cal_I`) Level and Between-Agent Spread

Table 2 compares the between-identity distribution of `cal_I(a)` across the `53`
identities with `externally_verified` pairs (`444` pairs; pairs per identity
`n = 53, mean = 8.3774, sd = 8.1478, p10 = 2.0000, p50 = 7.0000, p90 = 16.0000, iqr = 6.0000`)
and across all `113` paired identities (`662` pairs).

| Estimator variant | Corpus (`n` identities) | Mean `cal_I` | Between-agent SD | p10 | p50 | p90 | IQR |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Current deployed fleet `cal_I`** | `externally_verified` (`n = 53`) | `0.8526` | **`0.0000`** | `0.8526` | `0.8526` | `0.8526` | **`0.0000`** |
| **Deployed `#2194` candidate** (`>=5/bin`, `<=7d` causal on `snapshot.jsonl`) | `snapshot.jsonl` (`n = 0 / 318`) | — | — | — | — | — | — |
| Unpooled $1 - e_{\text{ece}}(a)$ | `externally_verified` (`n = 53`) | `0.8379` | `0.1075` | `0.7000` | `0.8500` | `0.9676` | `0.1945` |
| Unpooled $1 - e_{\text{brier}}(a)$ | `externally_verified` (`n = 53`) | `0.8734` | `0.0850` | `0.7620` | `0.8951` | `0.9775` | `0.1147` |
| Unpooled $1 - e_{\text{mae}}(a)$ | `externally_verified` (`n = 53`) | `0.7083` | `0.1098` | `0.5769` | `0.7000` | `0.8568` | `0.1333` |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.25, k = 5$)** | `externally_verified` (`n = 53`) | `0.8030` | **`0.0630`** | `0.7278` | `0.8000` | `0.8835` | **`0.0966`** |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.25, k = 10$)** | `externally_verified` (`n = 53`) | `0.7898` | **`0.0484`** | `0.7350` | `0.7833` | `0.8476` | **`0.0768`** |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.25, k = 20$)** | `externally_verified` (`n = 53`) | `0.7772` | **`0.0343`** | `0.7400` | `0.7700` | `0.8161` | **`0.0521`** |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.1474, k = 5$)** | `externally_verified` (`n = 53`) | `0.8508` | **`0.0551`** | `0.7804` | `0.8521` | `0.9263` | **`0.0783`** |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.1474, k = 10$)** | `externally_verified` (`n = 53`) | `0.8527` | **`0.0399`** | `0.8090` | `0.8523` | `0.9017` | **`0.0533`** |
| **Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.1474, k = 20$)** | `externally_verified` (`n = 53`) | `0.8535` | **`0.0265`** | `0.8220` | `0.8524` | `0.8847` | **`0.0313`** |
| Proposed pooled $1 - e_{\text{pool,ece}}$ ($e_0 = 0.25, k = 10$) | All exported (`n = 113`) | `0.7893` | `0.0410` | `0.7385` | `0.7869` | `0.8362` | `0.0492` |

**Key readout from Table 2:**
- Replacing the fleet constant (`between-agent SD = 0.0000, IQR = 0.0000`) with
  partial pooling over `externally_verified` pairs restores a between-agent
  `cal_I` SD of **`0.0399–0.0630`** (`IQR = 0.0533–0.0966` for $k \in \{5, 10\}$),
  translating at `eff_w = 0.3600` to **`0.0144–0.0227`** of direct between-agent
  `I` spread from the calibration component alone while shrinking single-pair
  noise toward the prior.
- Why `snapshot.jsonl` (`2026-09-14` to `2026-10-12`) has `0` causally prior
  `externally_verified` pairs: check-in `detail.confidence` stopped being
  emitted on `2026-09-13`, and check-in `self_report.confidence_reported` only
  began persisting on `core.agent_state` on `2026-10-12` (`#2741`, commit
  `ba1a90438`). Between `2026-10-12` and `2026-10-15`, `core.agent_state` has
  already accumulated `2,715` check-ins with `confidence_reported` across `35`
  identities, which will supply causal check-in-to-outcome pairs for the next
  offline read after the `2026-12-01` horizon.

### 4.3 Snapshot `I` Level and Threshold Coupling Across All `318` Identities

On `snapshot.jsonl` (`18,021` `I` observations across `318` identities), where
identities have $n_a = 0$ causal `externally_verified` pairs inside the
`2026-09-14..2026-10-12` capture gap, every identity evaluates at the
no-evidence prior $1 - e_0$:

| Calibration term on `snapshot.jsonl` (`318` identities, `18,021` rows) | Per-identity `I` mean | Per-identity `I` SD | p10 | p50 | p90 | IQR | Identities with mean `I >= 0.70` (`BASIN_I_HEALTHY`) | Rows with `I >= 0.70` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Current fleet `cal_I = 0.8526` ($e_0 = 0.1474$)** | `0.7012` | `0.0489` | `0.6284` | `0.7062` | `0.7605` | `0.0641` | `174 / 318` (**`54.7%`**) | `10,299 / 18,021` (**`57.1%`**) |
| **Neutral no-evidence prior `cal_I = 0.7500` ($e_0 = 0.2500$)** | `0.6625` | `0.0481` | `0.5915` | `0.6689` | `0.7229` | `0.0678` | `62 / 318` (**`19.5%`**) | `5,321 / 18,021` (**`29.5%`**) |

This confirms that `BASIN_I_HEALTHY = 0.70` is coupled to the `+0.3069` level
offset of the frozen `0.8526` fleet constant: dropping the prior from `0.8526`
to `0.7500` without re-centering `BASIN_I_HEALTHY` (or using the verified-pair
population ECE prior `1 - 0.1621 = 0.8379`) would push `35.2%` of the fleet
(`112 / 318` identities) below the healthy `I` basin boundary purely from a
prior shift.

---

## 5. Pre-Deployment Verification Gates

Before any runtime change replaces `get_mean_calibration_error()` in
`_compute_I_components`:

1. **Preservation horizon gate:** Complete the registered `2026-12-01`
   outcome-grounding stop-rule read (`#1425`) and operator-declared report
   finality ([`eisv-core-boundary-v0.md`](eisv-core-boundary-v0.md)).
2. **Post-`#2741` causal pair read:** Re-run
   `scripts/analysis/eisv_i_calibration_shadow_read.py` on an export window
   starting after `2026-10-12` (`ba1a90438`) so `checkin_self_report` supplies
   causal pre-committed forecasts on `core.agent_state` for `externally_verified`
   outcomes.
3. **Level & basin-boundary alignment:** Align $(e_0, \text{BASIN\_I\_HEALTHY})$
   so the no-evidence prior does not induce a spurious `35%` healthy-basin
   reclassification.
4. **3-consumer gate separation:** Keep the per-agent `I` calibration store
   strictly separate from the shared `0.65` gate in `outcome_events.py:643-702`
   and do not re-open supply into the legacy fleet bins.
