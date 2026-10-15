# Per-agent I calibration: scope audit and replacement design (v0)

**Status:** proposed design, 2026-10-15; documentation only, for [#2818](https://github.com/cirwel/unitares/issues/2818).
**Source audit:** `baabaf8b6692432a5d047823a70efe14664971ad`.
**Authority:** no estimator, producer, gate, threshold, flag, or data changes.
The offline comparison below is specified, not executed. Its parameters are
explicit exploratory choices, not an approved live estimator or a scientific
stop rule. The registered read and its preservation review still govern any
future change, including after the calendar reaches 2026-12-01.

## 1. Finding and scope

The deployed I observation gives `1 - fleet_calibration_error` 0.50 of its
base when at least three finite outcome scores exist, otherwise 0.60.
Continuity and tool-diversity blends retain 0.8 and 0.9 of that base when
present: the effective fleet weight is then 0.360 or 0.432. It is not always
0.360. Missing blends leave more of the fleet term in the observation.

The [existing frozen-data read](../../evaluations/eisv-ei-referent/result-v0.md)
reported one calibration error (0.1474) and 282 strategic samples throughout
its 30-day window ending 2026-10-12. It found no per-agent candidate data for
318 identities. Those are dated observations, not a fresh database audit.
The issue's later explanation of the supply cutoff does not turn the absence
of eligible records into absence of calibration capability or value.

A shared scalar contributes no between-agent information **at equal weights**.
It uses weight that could carry agent-specific information. Adding the same
constant does not change rank or absolute spread; dilution arises from the
weight left for the other terms. Different outcome branches, blend availability,
and observation times need not add the same constant, so rank invariance is
not a blanket claim for the deployed population. Compare those strata explicitly.

This audit covers the server-generated behavioral E/I/S observation, its V
consumer, continuity and drift suppliers, and adjacent ODE/control and baseline
paths. Caller-supplied/physical sensors bypass this behavioral construction;
their hardware producers are outside this audit. Source reachability does not
establish current deployment flags, nonzero values, or causal effect sizes.

## 2. Channel audit

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
| Baselines and common constants | Welford state is keyed by agent, not fleet; common bootstrap values, EMA settings, variance floors and fixed policy bands are shared design constants. Optional cohort seeds are an explicit cross-agent dependency. | `src/agent_behavioral_baseline.py:get_agent_behavioral_baseline`, `_maybe_seed_cohort_prior`; `src/behavioral_state.py`. Cohort prior is off by default; observe mode does not seed, apply mode does. No live flag read was made. |
| ODE rest target / downstream status | Class-scoped fixed S setpoints and common risk bands are not current per-agent evidence. | `src/monitor_setpoint.py`, `config/governance_config.py:get_s_setpoint`, `src/health_thresholds.py`. Record configuration in a future replay; do not interpret these constants as frozen measurements. |

**Audit conclusion:** fleet strategic I is not the only shared input. Tactical
drift and strategic ODE entropy forcing must be named in the preservation
inventory. Clipping, fixed defaults and self-loops are different mechanisms;
none is repaired by merely switching I's calibration source.

## 3. Verified evidence required before estimation

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

## 4. Proposed partial pooling rule

Use one versioned model per event family. Proposed offline settings are five
fixed bins `[0,.2), [.2,.4), [.4,.6), [.6,.8), [.8,1]`, a 90-day evidence
window, and prior strength `k = 10` pairs **per bin**. These choices are for
exploration; no accuracy threshold, policy authority or production approval
is attached to them. Record alternatives rather than selecting one for the
largest apparent spread.

Freeze a verified reference cohort before evaluation time `T0`. For each bin
`b`, compute the mean signed residual `mu_b = mean(p - y)` and the reference
bin frequency `q_b`. Use the same evidence contract as above. Exclude the
focal identity when constructing its reference prior (and its known duplicate
streams); keep that leave-identity-out prior fixed throughout evaluation.
No future evaluation outcomes may refresh it. If any required reference bin
has no evidence, mark the model `prior_unavailable`; do not borrow the current
mixed-proxy fleet bins or invent a zero residual. Record reference counts,
identity composition, cutoff, hashes and exclusions with the model version.

At a check-in at time `t`, use only that identity's pairs whose execution and
resolution are available before `t`, with resolution in `[t - 90 days, t)`:

```text
n_ab = number of admitted pairs for agent a in bin b
s_ab = sum(p - y) for those pairs
r_ab = (s_ab + k * mu_b) / (n_ab + k)
N_a  = sum_b n_ab

if N_a > 0:
    error_a = sum_b (n_ab / N_a) * abs(r_ab)
    term_a  = 1 - error_a
else:
    error_prior = sum_b q_b * abs(mu_b)
    term_a      = 1 - error_prior       # prior_only, never an agent measurement
```

The absolute value follows pooling of the signed bin residual; it is not
mean absolute error per event or Brier score. Opposing errors inside a coarse
bin can cancel, so retain bin counts, signed residuals and mean confidence /
observed frequency for inspection. Brier score can be reported separately as a
forecast-quality diagnostic but is not silently substituted for calibration.
Empirical bin weights change the estimand from the deployed unweighted mean;
the comparison below separates that arithmetic change from the scope change.

The agent-evidence fraction per bin is `n_ab / (n_ab + k)`: zero at no evidence,
0.5 at ten pairs, approaching one as evidence grows. Publish the weighted
prior fraction too. This replaces the existing fleet term in its slot; there
is no additional permanent fleet coefficient alongside it. Pooling deliberately
retains reference-cohort information when evidence is sparse and can compress
noisy differences at low counts. At `N_a = 0`, differences caused by the
leave-identity-out prior are prior composition effects, not current agent
evidence; report them separately from evidence-supported heterogeneity.

**No evidence:** expose `prior_only`, count zero, and an unassessed calibration
status. If the prior is unavailable, the candidate term is null. A replay may
retain the deployed term on such rows only as an explicitly labelled fallback,
excluded from estimates of candidate performance. Never describe either case
as measured good calibration. If evidence ages out, the model returns to the
same prior-only state; publish the newest evidence time and expiry counts.
No live fallback or threshold behavior is authorized here.

For the first offline intervention, replace only `calibration_accuracy`'s
value inside `_compute_I_components`; keep the original 0.50/0.60 branch,
coherence/outcome terms, clipping and ordered 0.20/0.10 blends fixed. Do not
simultaneously change outcome consistency, tactical drift or ODE forcing.

## 5. Offline comparison to run before any live change

This is a descriptive measurement comparison, not the outcome-prediction read
of [#1425](../registered/eisv-outcome-grounding-stop-rule-v0.md), not an
individuality-v3 test, and not permission to rerun a stopped experiment.
No new run is claimed by this PR.

1. **Freeze and inventory.** Preserve the existing October 12 artifacts and
   their published hashes unchanged. Add a separate manifest for any eligible
   verified-pair export: input hashes, schema, source commit, configuration,
   UTC windows, producer trust evidence, ordering fields, missingness and every
   exclusion count. Use offline files only. First audit whether those fields
   exist; a legacy grade, nearby confidence or timestamp alone cannot fill them.
   A missing reference prior or insufficient verified pairs is an unavailable
   comparison, not a reason to reopen supply or fabricate a result.
2. **Pin before looking.** Freeze `T0`, evaluation end, prior cohort, family,
   duplicate-stream grouping and model settings in the run manifest before
   computing contrasts. Use the pre-`T0` reference only. Sensitivity settings
   `k = 5, 20` and windows 30/180 days are reported alongside 10/90, without
   selecting a winner from spread. These are proposed exploratory settings,
   not changes to any registered protocol.
3. **Recover the deployed observation.** Reconstruct each available sensor I
   from persisted components, original branch and ordered blends. Report parity
   residuals and missing derivations; never silently fill them. Retain identity,
   time, source, outcome-score count and optional-blend availability. Restrict
   comparisons to the server-generated behavioral path.
4. **Paired arms.** A: exact deployed fleet term. B: verified reference-only
   term. C: proposed agent partial-pooling term. D: same verified agent evidence,
   unpooled, with the same empirical bin weights (diagnostic only). E: the
   legacy unweighted eligible-bin arithmetic on verified agent evidence, with
   its five-sample and seven-day recent-activity eligibility. B versus A
   exposes reference/level change; C versus B exposes agent evidence; C versus
   D exposes pooling; D versus E exposes weighting and eligibility. For D/E
   report both own coverage and their common-support comparison. Keep all
   other inputs fixed, and separate `observed`, `prior_only`, `prior_unavailable`
   and explicit deployed fallback rows in every table.
5. **Report levels and spread separately.** For the calibration slot and raw I,
   publish equal-identity distributions of identity means (SD, IQR, p10/p50/p90),
   paired per-identity mean shifts, within-identity SD, and row-weighted versions
   alongside them. Stratify by event family, evidence count (0, 1–4, 5–19, 20+),
   prior fraction, outcome branch, blend availability and time coverage. Compare
   overlapping calendar support as well as full-window means; high-cadence
   identities must not silently define the fleet. Report rank changes on common
   support as descriptive only. Greater spread alone is not better measurement.
6. **Scope downstream claims.** Fixed-input replacement identifies the immediate
   observation delta. A separate chronological replay can fold identical initial
   BehavioralState snapshots through unchanged EMA and V equations, reporting
   I/V levels and baseline deviations. Missing initial state or inputs means
   replay unavailable. Holding future inputs fixed does not simulate changed
   verdicts, histories, outcomes or ODE feedback. Threshold-distance summaries
   are descriptive exposure, not actual policy effects or benefits.
7. **Preservation disposition.** Publish the report, manifest and unavailable
   strata; update the tested-claims ledger and evaluation index with measured
   results only. Review distribution shifts, verified-source adequacy, recursive
   dependencies and preservation constraints before proposing any live change.
   The date alone does not release the predictor. No outcome-validity or
   incentive-compatibility claim follows from this comparison.

The earlier shadow paired a label with the most recent same-identity confidence
within 24 hours and found no eligible substitution. It remains an exploratory
coverage result. Its `externally_verified` / `tool_observed` labels and temporal
proximity do not establish this note's prospective, target-bound, independently
resolved pairs. Future dense confidence capture does not by itself repair that.

## 6. Completion and outstanding dependencies

The source audit, replacement rule, cold-start behavior, required source and
offline comparison specification satisfy the documentation scope of #2818.
Ledger row 49 and `docs/EVALUATION_INDEX.md` record that scope. Verified supply,
comparison execution and preservation approval remain outstanding; there is
no runtime implementation or live-estimator change in this proposal.
