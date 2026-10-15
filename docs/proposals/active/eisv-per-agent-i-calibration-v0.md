# Per-Agent EISV `I` Calibration Term and Cross-Channel Homogenization Audit (v0)

**Status:** Proposed design and exploratory legacy-pair comparison (`#2818`), 2026-10-15.
Verified-pair replacement efficacy is untested; section 6 specifies the required
comparison. Historical tables below are retained, not newly measured here.
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
**Design decision (`#2818`, 2026-10-15; made by agents on operator delegation,
reversible):** no constant without a referent. Calibration is reported first as
its own channel beside `I` (section 3.0). With no admissible evidence it reports
`unavailable`; no prior value enters `I` or the channel (section 3.2). Pooling
toward a prior is allowed only when that prior is estimated from preserved
eligible evidence. Coupling between agents is measured separately, never added
into another agent's state (section 3.4). Whether a verified calibration term
re-enters `I` is a post-read choice that the section 6 comparison informs.
Parent issue: `#2610`.

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
whose deployed return averages `calibration_checker.compute_calibration_metrics()`
over fleet-wide strategic bins with `count >= 5`.

Two distinct defects compound on this path:

1. **Structural homogenization (design-intent defect, `#2818`):** Even when the
   fleet strategic bins were actively receiving samples (prior to `2026-08-02`),
   `get_mean_calibration_error()` returned a single fleet-wide scalar on every
   check-in. Injecting a fleet scalar at `36%–43%` effective weight into a
   per-agent channel leaves less weight for agent-specific information. At
   equal weights a common additive constant changes neither ranks nor absolute
   spread; dilution concerns the remaining weights. Different outcome branches,
   optional blends and observation times need not preserve rank.
2. **Frozen upstream supply (ledger rows 15, 48):** After `#1445` (`2026-08-02`)
   excluded server-scraped confidence (`prev_confidence_fallback` and
   `audit_trail_fallback`) from training calibration, the fleet strategic bins
   froze at `282` samples (`calibration_error = 0.147446577`, `cal_I = 0.852553423`).
   Across all `18,021` non-synthetic `I` observations (`318` identities) in the
   30-day window ending `2026-10-12`, `cal_I` had **zero** within-agent and
   **zero** between-agent variance.

---

## 2. Audit of Per-Agent Channels for Fleet-Scoped or Frozen Inputs

Source inspection at `baabaf8b6692432a5d047823a70efe14664971ad` covers
the behavioral sensor and its suppliers and consumers. Caller-supplied physical
sensors bypass that construction; their hardware producers are outside scope.
Dated deployment findings in earlier ledger rows are not fresh measurements.

All paths below are relative to the repository root. Symbols, rather than line
numbers, identify the inspected implementation at the pinned commit.

| Channel / consumer | Scope and constant risk | Source and consequence |
|---|---|---|
| I: `calibration_accuracy` | Fleet strategic bins; frozen in the cited window. Missing error defaults to 0.75. | `src/mcp_handlers/updates/context.py:get_mean_calibration_error`; `src/behavioral_sensor.py:_compute_I_components`. This is the term to replace. |
| Drift calibration → behavioral S | **Additional fleet input:** worst positive tactical confidence/accuracy gap among bins with at least 10 samples. No agent argument. | `src/monitor_drift.py:compute_calibration_error` / `compute_drift_vector` pass it to `governance_core/ethical_drift.py:compute_ethical_drift`. Warmup, task context and velocity can modify the component; the previous drift norm supplies 40% of S's base. Fleet scope is verified; a frozen/nonzero deployed value is not established here. |
| ODE S forcing | **Additional fleet input:** maximum damped positive strategic gap, bins with at least two samples; full sample weight at five. | `src/governance_monitor.py:update_dynamics` passes `calibration_penalty` as `noise_S` to `step_state`. This is ODE forcing, not a direct addition to behavioral S. It can propagate through legacy control feedback; magnitude and downstream effects need replay. |
| E/I coherence; drift coherence | Agent-local ODE state transformed into legacy directional feedback, not fleet calibration itself; narrow-range or default signals can compress variation. | `_compute_E_components`, `_coherence_trend` and `compute_drift_vector` consume `C(V_ODE)` level/trend/deviation. Coherence fallback is 0.6 in the sensor; trend needs four history entries. Existing coherence ablations remain separate. |
| E: decision history | Agent-local policy outputs with a shared fixed score map. A repeated verdict produces a stable component. | `_compute_E_components` scores the last ten verdicts, with 0.35/0.40 base weight. This is a feedback loop, not an independent outcome or a fleet aggregate (ledger rows 45/47). |
| S: regime instability | Agent-local regime history, potentially a self-loop through the state it labels. Fewer than two entries defaults to 0.1. | `_compute_S_components` / `_regime_instability`; 35% base weight. The fleet-measured 0.12 neutral value belongs only to the existing shadow, not live S (ledger row 46). |
| Continuity activity → E | Per-agent text/cadence, but can clip to a shared 0.3 floor. | `src/dual_log/continuity.py:compute_continuity_metrics` divides token rate across the whole check-in gap by 200, then clips. Its `E_input_clipped` canary records saturation. No new clipping-frequency estimate is claimed. |
| Complexity divergence → E/I/S | Per-agent report/text comparison; omitted reports and fixed templates can make it constant. It is not the calibration singleton. | `derive_complexity` / `compute_continuity_metrics`; `I_input = 1 - divergence`. Sensor defaults use divergence 0.15 for E and 0.1 for S; continuity's missing-report branch uses 0.2. These are distinct stages. `self_complexity_defaulted` and `continuity_degenerate` describe coverage. |
| Drift stability → S / ODE | **Fixed default on this call path:** `decision=None` leaves fresh baseline decision consistency at 0.8, hence stability deviation 0.2 before modifiers. | `compute_drift_vector`, `AgentBaseline.update`, `compute_ethical_drift`. Restored baselines need separate inspection; this does not assert every historical row is 0.2. Ledger row 37 describes earlier measurements. Missing tactical evidence instead uses agent-relative confidence changes, not zero error. |
| Outcome success (E), score consistency (I) | Query is agent-scoped, last 20 / 24h. Identical outcome scores yield maximal consistency even when all outcomes are bad. | `phases.py` calls `get_recent_outcomes(agent_id=...)`; `_compute_I_components` uses variance, not correctness. Current provenance filtering and its supply gates remain unchanged. |
| Tool errors (E), velocity (S), diversity (I) | Agent-scoped one-hour statistics, even though the tracker is a singleton. Constant success or sparse usage can limit variation. | `phases.py` calls `get_usage_stats(agent_id=..., window_hours=1)`. With no calls, optional blends are absent; that is not an observed zero error rate. |
| Behavioral V | Agent-local EMA of raw `E_obs - I_obs`; inherits I's shared input. | `src/behavioral_state.py:BehavioralState.update`. Sensor `_compute_V_components` is diagnostic and is not this V's producer. Replacing I therefore also changes V and its baseline over time. |
| Omitted confidence | Derived from the agent state using shared weights, including 55% legacy coherence. It can inherit upstream shared inputs; no new frequency or saturation claim is made. | `src/confidence.py:derive_confidence`. A derived fallback is not the agent's forecast. |
| Baselines and common constants | Welford state is keyed by agent, not fleet; common bootstrap values, EMA settings, variance floors and fixed policy bands are shared design constants. Optional cohort seeds are an explicit cross-agent dependency. | `src/agent_behavioral_baseline.py:get_agent_behavioral_baseline`, `_maybe_seed_cohort_prior`; `src/behavioral_state.py`. Cohort prior is off by default; observe mode does not seed, apply mode does. No live flag read was made. |
| ODE rest target / downstream status | Class-scoped fixed S setpoints and common risk bands are not current per-agent evidence. | `src/monitor_setpoint.py`, `config/governance_config.py:get_s_setpoint`, `src/health_thresholds.py`. Record configuration in a future replay; do not interpret these constants as frozen measurements. |

**Audit conclusion:** fleet strategic I is not the only shared input. Tactical
drift and strategic ODE entropy forcing must be named in the preservation
inventory. Clipping, fixed defaults and self-loops are different mechanisms;
none is repaired by merely switching I's calibration source.

---

## 3. Design of the Per-Agent `I` Calibration Replacement

### 3.0 Placement: Its Own Channel First

Calibration resolves sparsely and late. Behavioral `I` is a per-check-in
observation. Folding the first into the second forces a value onto every check-in,
including those of agents with no evidence, and that forced value is how the fleet
scalar homogenized `I`. The first deliverable is therefore a reported,
measurement-only channel beside `I`. It carries `(value, coverage, n_verified_pairs,
newest_pair_at, evidence_status)` and changes no `I` observation, weight, threshold
or verdict. It is the target-bound successor to the `#2194` candidate.

Rule 1 below describes the in-slot replacement as one option for after the read.
Section 6 compares it with a calibration-free `I` (arm F) before anyone proposes
either for live use. Two outside model families recommended the separate channel
independently in the advisory consultation recorded on `#2818`. That
recommendation is advice, not evidence.

### 3.1 Pooling Rule: Replace the Fleet Term via Partial Pooling

**Rule 1 (Replace, do not blend; post-read option):** `cal_I(a, t)` replaces `1.0 - calibration_error`
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
toward a prior error $e_0 \in (0, 1)$ with pseudocount $k > 0$ (exploratory
setting $k = 10$, not an approved live default):

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
1. **The reported channel is `unavailable`.** Its value is null, with
   `evidence_status = "unavailable"`, `default_reason = "no_verified_agent_pairs"`
   and `n_verified_pairs = 0`. No prior value is reported and none enters `I`.
   A shared prior given to every agent without evidence would bring back the
   homogenization this note exists to remove.
2. **Prior-only values exist only as a comparison arm.** In section 6, arms B
   and C compute $1.0 - e_0$ at $n_a = 0$ so their effects on level and spread can
   be measured. Those rows are labelled explicitly as prior-only rather than as
   observed calibration:
   - `source = "agent_calibration_prior"` (vs `"agent_calibration_pooled"` when $n_a \ge 1$),
   - `observed = False`,
   - `evidence_status = "prior_only"`, `pooling_weight = 0.0`.

   A prior-only value is never published as an agent's calibration.
3. **Prior provenance and missing prior:** For the verified-pair comparison,
   freeze a reference cohort before evaluation time `T0`, excluding the focal
   identity and known duplicate streams. Compute `e_0` as the equal-identity
   mean of reference identities' sample-weighted binned ECE for the same event
   family. Record cohort counts, cutoff, exclusions and artifact hashes. Keep
   this prior fixed for the whole evaluation; later agents' outcomes cannot
   change it. If no eligible reference exists, return a null candidate with
   `prior_unavailable`, not zero error or the legacy fleet scalar. No-evidence
   `prior_only` rows remain unassessed. Differences between leave-identity-out
   priors are composition effects, not current agent evidence.
4. **Historical sensitivity settings:** Section 4's `e_0 = 0.25` is the
   existing missing-error fallback; `e_0 = 0.1474` reproduces the historical
   fleet level. Neither is established as a verified population prior. Its
   `0.1621` mean comes from exploratory temporal pairs and is not eligible
   reference evidence under section 3.3.
5. **Threshold exposure:** Level shifts must be reported separately from
   spread. They do not mandate re-centering `BASIN_I_HEALTHY`, prove its tuning
   history, or establish actual basin changes: the snapshot is a behavioral I
   observation, while the deployed basin classifier reads ODE state. Any
   future estimator or threshold choice needs separate preservation review.

### 3.3 Verified-Outcome and Pre-Committed Forecast Requirements

The replacement estimates calibration for a declared binary event family,
not generic competence, integrity, or an incentive for honest reporting.
A command-exit forecast is one possible family; do not mix it silently with
PR acceptance, internal trajectory scores or soft task labels.

Each admitted pair must have:

1. A durable forecast id, accountable process identity, original probability
   `p` in [0,1], explicit event/execution target and event-family version.
   Commitment must be verifiably before execution starts or the outcome becomes
   knowable, not merely before an outcome POST reaches the server.
2. A server-validated binary result `y`, established by an authenticated,
   independent resolver outside the forecaster's write authority. For command
   exits, require an execution nonce, full command/environment identity, start
   and finish evidence and exit status. Remote CI is a possible source only
   after its identity, run/artifact binding and trust boundary are reviewed.
3. Exactly one canonical resolution per forecast/execution, atomic and durable;
   duplicates, conflicting labels, post-hoc probabilities and unresolved or
   expired forecasts are separately counted. Unresolved forecasts have no
   invented success/failure label. Report resolution coverage and selection bias.
4. Availability timestamps and provenance sufficient for an as-of replay. A
   later correction cannot silently rewrite an earlier estimator input.

This source is a **dependency, not an existing verified supply guarantee**.
`src/outcome_corroboration.py:tool_observation_triggers` accepts caller-described
return codes and payload shapes for `TOOL_OBSERVED`. A grade or weight >=0.65
alone does not prove truth. Registry binding alone does not prove truth either.
The [forecast-bound proposal](forecast-bound-calibration-mirror-v0.md), C2 and
section 5, explains the shared-OS-user producer problem. Reusing its proposed
receipts requires independent authorization and trust-boundary work; this note
does not reopen its incentive proposal or authorize a new tool.

The current outcome-event gate in
`src/mcp_handlers/observability/outcome_events.py` feeds strategic bins,
tactical bins for hard-exogenous types, and (when eligible) the sequential
tracker. **Do not loosen or repoint that gate, backfill its bins, or reopen
fleet supply to implement this design.** A future verified-pair dataset must
be isolated from those consumers until each change is separately reviewed.
The existing `compute_agent_calibration_candidate` remains a measurement-only,
unweighted strategic-bin diagnostic with mixed proxy evidence; it is not this
replacement.

### 3.4 Coupling Between Agents Is Measured, Not Added

Before 2026-08-02, while fleet bins were filling, an outcome recorded for one agent
moved every agent's `I`. That was coupling with no scope: nobody declared it and
nothing attributed it. Partial pooling is statistical sharing, not coordination.
Where coordination between agents is the object of interest, measure it directly:

- **Edges:** declared work relations only, where agent B consumes, reviews or
  builds on agent A's output (a PR review, a dependent task, a dialectic pair).
  Co-location and shared queue position are not edges.
- **Quantity:** directed influence. Record B's target-bound forecast before and
  after B receives a specified signal from A, then score both against the same
  independently resolved outcome. Report forecast movement and the change in
  score, with uncertainty, per edge.
- **Limits:** this measures response to a signal. A causal benefit claim needs
  controlled exposure. The result is reported as its own reading and never
  written into either agent's E/I/S/V.

Like section 3.3's supply, this is a dependency. The note specifies it and
authorizes no tool or schema.

---

## 4. Offline Comparison of Between-Agent Spread and Level on Preserved Data

Criterion 3 of `#2818` requires specifying an offline comparison, to be run
before any live change. This section retains the earlier exploratory arithmetic
on historical temporal pairs. It does not satisfy the verified-pair contract
in section 3.3; the required comparison protocol is specified in section 6.

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
pairs per identity). Stratifying by stored `corroboration_grade` describes these two selected
corpora; their error difference does not establish why they differ or prove
either corpus independently verified:

| Outcome grade | Paired rows (`n`) | Identities | Pairwise $\|y - c\|$ mean | Pairwise $\|y - c\|$ p10 | Pairwise $\|y - c\|$ p50 | Pairwise $\|y - c\|$ p90 |
|---|---:|---:|---:|---:|---:|---:|
| `externally_verified` (stored grade only) | `444` | `53` | `0.2881` | `0.1000` | `0.2000` | `0.7000` |
| `tool_observed` (`0.65` shape-graded) | `218` | `60` | `0.0922` | `0.0100` | `0.0400` | `0.1800` |

### 4.2 Per-Identity Calibration Component (`cal_I`) Level and Between-Agent Spread

Table 2 compares the between-identity distribution of `cal_I(a)` across the `53`
identities with `externally_verified` pairs (`444` pairs; pairs per identity
`n = 53, mean = 8.3774, sd = 8.1478, p10 = 2.0000, p50 = 7.0000, p90 = 16.0000, iqr = 6.0000`)
and across all `113` paired identities (`662` pairs).

| Estimator variant | Corpus (`n` identities) | Mean `cal_I` | Between-agent SD | p10 | p50 | p90 | IQR |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Current deployed fleet `cal_I`** | `externally_verified` (`n = 53`) | `0.8526` | **`0.0000`** | `0.8526` | `0.8526` | `0.8526` | **`0.0000`** |
| **Measurement-only `#2194` candidate** (`>=5/bin`, `<=7d` causal on `snapshot.jsonl`) | `snapshot.jsonl` (`n = 0 / 318`) | — | — | — | — | — | — |
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
  partial pooling over legacy pairs labelled `externally_verified` produces a between-agent
  `cal_I` SD of **`0.0399–0.0630`** (`IQR = 0.0533–0.0966` for $k \in \{5, 10\}$),
  translating at `eff_w = 0.3600` to **`0.0144–0.0227`** of direct between-agent
  `I` spread from the calibration component alone while shrinking single-pair
  variation toward the prior. Greater spread alone is not measurement validity.
  This is a component-only scaling, not the SD of full I, whose other terms
  covary with calibration and whose effective weights differ by row.
- The earlier read reported no eligible legacy candidate substitution in the
  snapshot window. That eligibility failure is not proof of zero temporal pairs
  under a different estimator, and denser confidence capture does not establish
  prospective target-bound forecasts. The revised verified-pair comparison
  must first audit coverage under section 3.3 rather than inherit the old count.

### 4.3 Snapshot `I` Level and Threshold Coupling Across All `318` Identities

On `snapshot.jsonl` (`18,021` I observations across `318` identities), the
following is an explicit **all-prior sensitivity**: substitute the same prior
for every row while holding the other inputs fixed. It does not estimate the
proposed verified-pair model's actual coverage or trajectory.

| Calibration term on `snapshot.jsonl` (`318` identities, `18,021` rows) | Per-identity `I` mean | Per-identity `I` SD | p10 | p50 | p90 | IQR | Identities with mean `I >= 0.70` (`BASIN_I_HEALTHY`) | Rows with `I >= 0.70` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Current fleet `cal_I = 0.8526` ($e_0 = 0.1474$)** | `0.7012` | `0.0489` | `0.6284` | `0.7062` | `0.7605` | `0.0641` | `174 / 318` (**`54.7%`**) | `10,299 / 18,021` (**`57.1%`**) |
| **Neutral no-evidence prior `cal_I = 0.7500` ($e_0 = 0.2500$)** | `0.6625` | `0.0481` | `0.5915` | `0.6689` | `0.7229` | `0.0678` | `62 / 318` (**`19.5%`**) | `5,321 / 18,021` (**`29.5%`**) |

These are fixed-input raw-I level comparisons against the numeric reference
0.70. They are not live basin classifications or a count of false pauses.
The snapshot's 112/318 shift is a threshold-exposure diagnostic, not authority
to move a threshold. The prior/reference choice and recursive downstream
behavior require separate evaluation and preservation disposition.

---

## 5. Pre-Deployment Verification Gates

Before any runtime change replaces `get_mean_calibration_error()` in
`_compute_I_components`:

1. **Preservation horizon gate:** Complete the registered `2026-12-01`
   outcome-grounding stop-rule read (`#1425`) and operator-declared report
   finality ([`eisv-core-boundary-v0.md`](eisv-core-boundary-v0.md)).
2. **Verified source:** Establish the target-bound independent evidence channel
   in section 3.3. Dense check-in confidence and higher corroboration grades
   alone do not satisfy it. No new source or tool is authorized by this note.
3. **Offline evaluation:** Complete section 6 on preserved eligible evidence,
   report missing coverage and level shifts, and review downstream exposure.
   Do not automatically re-center thresholds to retain a historical population.
4. **Consumer separation:** Leave the shared strategic/tactical/sequential
   supply gate and legacy bins unchanged. Any future change to those consumers
   requires separate review.

## 6. Verified-pair offline comparison protocol (not run)

This is an exploratory measurement comparison, not a rerun of #1425 or an
individuality-v3 test. No live query, new experiment or predictor change is
implemented here. The exploratory choices below are stated before their use;
no pass/fail threshold or deployment authority is attached to them.

1. **Freeze inputs.** Preserve section 4's artifacts unchanged. Create a
   separate manifest for eligible verified pairs: input hashes, audited source
   commit, source/target schemas, producer trust evidence, ordering fields,
   exclusions and resolution coverage. Count unresolved, expired, duplicate,
   conflicting and corrected records separately; never invent missing labels.
   If the required provenance is absent, report the comparison unavailable.
2. **Freeze the model.** Before computing contrasts, pin `T0`, evaluation end,
   reference cohort, event family, identity/duplicate grouping, five bins from
   section 3.1 and `k = 10`. Use only pre-`T0` reference evidence for `e_0`
   (section 3.2). The primary window is lifetime evidence, matching section 3.1,
   available before each check-in; record newest-evidence age. Report `k = 5, 20`
   and 30/90-day trailing resolution windows as sensitivities, not as a search
   for the largest spread. Finite windows can revert to prior-only as evidence
   ages out; label expiry counts. Do not silently refresh the prior.
3. **Recover deployed observations.** Reconstruct persisted raw I from its
   components and ordered blends; report parity residuals and missing records.
   Keep original 0.50/0.60 branch, other terms, clipping and optional blends.
   Restrict the comparison to the server-generated behavioral path. Do not
   change tactical drift, ODE forcing or outcome consistency at the same time.
4. **Paired arms.** A: deployed fleet term. B: frozen verified reference-only
   term. C: section 3.1 partial pooling. D: unpooled agent ECE on the same
   verified evidence. E: legacy unweighted eligible-bin arithmetic on that
   evidence, including its five-sample/seven-day eligibility. F: calibration-free
   `I`, a separately named candidate with the calibration slot removed and the
   remaining base weights renormalized explicitly and recorded, so it cannot be
   mistaken for the deployed `I`. Arm F is reported alongside the section 3.0
   channel. B–A separates reference/level change, C–B agent evidence, C–D
   pooling, D–E weighting and eligibility, and F–A what the fleet term
   contributes. For D/E show own coverage and common-support results.
   Keep observed, prior-only and unavailable rows distinct. A deployed-value
   fallback may be displayed but is not evidence of candidate performance.
5. **Levels and spread.** For the slot and raw I report equal-identity
   distributions of identity means (SD, IQR, p10/p50/p90), paired mean shifts,
   within-identity SD and row-weighted versions. Stratify by event family,
   evidence counts (0, 1–4, 5–19, 20+), prior weight, outcome branch, optional
   blends and calendar coverage. Compare overlapping-time support as well as
   full-window means. Rank changes are descriptive. More spread is not by
   itself evidence of better calibration or a meaningful agent difference.
6. **Downstream limits.** Immediate replacement measures a fixed-input raw-I
   delta. A separate chronological replay may apply unchanged BehavioralState
   I/V EMA and baseline equations from identical preserved initial states;
   missing initial states/inputs make that replay unavailable. Holding future
   inputs fixed does not simulate changed verdicts, histories, outcomes or ODE
   feedback. Raw-I distances from 0.70 cannot be reported as live basin flips.
7. **Publish and review.** Publish the manifest, unavailable strata and results
   in the ledger and evaluation index. Independently review evidence adequacy,
   distribution shifts and preservation constraints before proposing a live
   change. The calendar date alone does not release the predictor. No
   outcome-validity or incentive-compatibility claim follows from these arms.
