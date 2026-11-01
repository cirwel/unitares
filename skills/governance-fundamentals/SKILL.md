---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, policy actions, coherence, calibration. Reference material for interpreting
  governance metrics as proprioceptive state estimation, not outcome judgment.
last_verified: "2026-10-29"
freshness_days: 21
source_files:
  - unitares/config/governance_config.py
  - unitares/src/agent_loop_detection.py
  - unitares/governance_core/coherence.py
  - unitares/governance_core/parameters.py
  - unitares/src/auto_ground_truth.py
  - unitares/src/governance_monitor.py
  - unitares/src/monitor_calibration.py
  - unitares/src/governance_glossary.py
  - unitares/src/behavioral_state.py
  - unitares/src/behavioral_sensor.py
  - unitares/src/mcp_handlers/updates/context.py
  - unitares/docs/EISV_COMPUTATION.md
  - unitares/src/behavioral_assessment.py
  - unitares/src/cold_start_risk_confirmation.py
  - unitares/src/monitor_decision.py
  - unitares/src/monitor_metrics.py
  - unitares/src/monitor_result.py
  - unitares/src/coherence_provenance.py
  - unitares/src/confidence.py
  - unitares/src/eisv_telemetry.py
  - unitares/src/services/runtime_queries.py
  - unitares/src/mcp_handlers/response_formatter.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/lifecycle/recovery_policy.py
  - unitares/src/mcp_handlers/dialectic/enforcement.py
  - unitares/src/mcp_handlers/observability/outcome_events.py
---

# Governance Fundamentals

## What UNITARES Is

UNITARES is accountability infrastructure for long-running AI agents: a
self-hosted, single-operator federation kernel for agent identity, claims and
evidence, review, outcomes, and reconstruction. This skill covers one instrument
inside it, the check-in's state estimate.

That instrument provides digital proprioception for AI agents — awareness of your own state, your relationship to the system, and whether you are drifting. The live path is behavioral state estimation: observable work signals become EISV readings, smoothed over time and compared with the agent's own trajectory once a baseline exists.

## EISV State Vector

The behavioral path produces four coordinates from three blended observation
streams, updated through check-ins:

| Dimension | Range | What the current computation records |
|-----------|-------|---------|
| **E** (Energy) | [0, 1] | Weighted observations and controller history: prior decisions, coherence level, complexity divergence, and available outcome, continuity and tool features |
| **I** (Information Integrity) | [0, 1] | Calibration error, coherence trend, and available outcome-score consistency, continuity and tool-variety features |
| **S** (Entropy) | [0, 1] | Drift, regime transitions, complexity divergence, and available continuity and tool features |
| **V** (Valence) | [-1, 1] | Separately EMA-smoothed gap between raw E and I observations |

These are weighted indices with historical names. Productive capacity,
information integrity and entropy are intended interpretations, not additional
quantities directly measured by these formulas. A narrow, derived or correlated
signal is still information that can be retained and inspected. Its definition,
diagnostic interpretation, usefulness and policy role are separate questions.
For the formulas and producer distinctions, see the repository's
`docs/EISV_COMPUTATION.md`.

### How the Live Path Reads Them

- **E (Energy)** blends scores assigned to prior governance decisions, complexity divergence, recent recorded outcomes, and a legacy coherence-level term sourced from `legacy_tanh_v` (see *Coherence*), with optional continuity and tool-success inputs. Because prior decisions enter E, a policy response can change later E even when task and tool features stay fixed. That reflects controller feedback; it does not independently confirm the condition that prompted the decision. The outcome term applies once three or more outcomes are recorded in the last 24h. It counts them as recorded, of any provenance, without attribution to whether your change caused them, and an adverse one means rework, not fault. When an adverse outcome counts, the check-in carries `outcomes_in_e` saying which outcomes counted, the term's share of the observation, and what one more adverse outcome would cost.
- **I (Integrity)** blends calibration error and outcome-score consistency with the trend of that same legacy controller scalar, plus optional continuity and tool-variety inputs. Outcome consistency measures variance: constant success and constant failure scores both have zero variance. Outcome success enters E separately. The live handler's calibration input comes from lifetime fleet strategic bins, not an error measurement for the current claim; an agent-scoped candidate is measurement-only. The monitor fallback uses tactical overconfidence instead.
- **S (Entropy / drift)** rises with drift norm, regime instability, and complexity divergence.
- **V (Valence)** smooths `E_obs - I_obs` once, with its own EMA rate. Positive records a positive smoothed `E_obs - I_obs` gap; negative records the reverse. Its different smoothing rate means V is not generally the difference of the current smoothed E and I values. It adds a temporal summary, not another external observation. "Running hot" and "running careful" are interpretations of the gap.

The headline math is proprioceptive residuals. The live estimation stages are
explicit: check-ins 1–2 use the mostly server-derived Φ cold-start prior;
check-ins 3–24 use the behavioral assessment against fixed universal
thresholds; check-in 25 enables self-relative z-score deviation against the
agent's Welford baseline (the stage boundaries derive from
`BEHAVIORAL_AUTHORITY_THRESHOLD = 0.3` over `BOOTSTRAP_UPDATES = 10`, and
`is_baselined` at `baseline_confidence >= 0.8` with the confidence ramp
`(update_count - 5) / 25`). Absolute safety floors and basin-health gates
remain in force throughout.

The roadmap target for richer cold-start grounding (a class-anchor reference
blend) is in `docs/ontology/eisv-proprioception-contract.md`, *Core
mathematical posture*; it is not the live path.
Deviation is information first, not a guilty verdict. Policy can recommend
`guide`, `pause`, or `reject`, but EISV itself is measurement/diagnosis, not
prosecution. In the default `observe` posture those recommendations cannot
change lifecycle state or block writes.

Prefer live tool output over static range lore if the current runtime reports a narrower or more precise bound.

## Basins

Your state sits in a basin — a region of the EISV space:

- **High basin**: Healthy. E and I are high, S and V are low. Normal operating range.
- **Low basin**: Degraded. May need recovery or intervention.
- **Boundary**: Transitioning between basins. Extra attention from governance — the verdict's
  sub-action is `guide` regardless of the headline. The basin does **not** force `margin: tight`:
  `basin` and `margin` are two different notions of "edge", and the boundary condition is carried
  by `basin`, the `guide` sub-action, and the guidance text, not by the margin enum.

Use `check_working_state(client_session_id=..., verbosity="standard")`
(`get_governance_metrics()` canonically) as the source of truth for the current basin/mode labels rather
than assuming they are constant across runtime versions; the default
`check_working_state()` envelope leaves them out.

When a response includes `policy_evaluation.inputs.basin`, read it as the
decision-time policy basin. Agent-facing state fields can be sourced from the
primary EISV path for that response, so newer responses also include
`policy_basin`, `policy_basin_source`, and `primary_eisv_source` to make that
measurement distinction explicit (the compact formatter keeps `basin`,
`policy_basin`, and `primary_eisv_source` and drops `policy_basin_source`).

## Verdicts

Each check-in records three different things: `policy_evaluation.action` is the
raw inferred recommendation; top-level `action` is the effective operational
directive; and `enforcement.applied` says whether lifecycle authority actually
crossed the boundary. The separate `verdict` field is a glossary wrapper
(`{value, meaning, next_action, …}`), not any of those. Never read
`verdict.value` as the action.

| Action | Sub-action | Meaning | What to do |
|--------|-----------|---------|------------|
| **proceed** | `approve` | State is healthy | Continue working normally |
| **proceed** | `guide` | Something is slightly off | Read the guidance text, adjust approach |
| **proceed** | `guide` with `recommended_action=pause` | Default observation posture: an inferred edge was crossed, but no hold was applied | Inspect if useful; continuing is neither permission nor a safety certification |
| **pause** | named pause with `enforcement.applied=false` | Enforcement posture returned the compatibility directive, but this path did not commit a lifecycle hold | Do not infer a write gate; inspect enforcement or recovery state |
| **pause** | `reject` or a named pause, with `enforcement.applied=true` | Explicit compatibility actuator applied a lifecycle hold | Stop and use the named recovery, optional dialectic review, or human input |

Separately, `metrics.verdict` may carry an internal UNITARES verdict such as
`safe` / `caution` / `high-risk`. Read it as interpreted state/context, not as
moral judgment. The behavioral assessment owns the main risk/verdict path after
the two-check-in prior window. Coherence, CIRS, basin, and adaptive-governor
compatibility backstops remain part of that inference, but they actuate only
when the operator sets `UNITARES_POLICY_ACTUATION=enforce` (see *Coherence*).

On read-only metrics, headline `risk_score` and `status` use the risk that
actually produced the last verdict when available. Inspect `risk_score_source`:
`resolved` means that verdict risk, while `phi_history` is the honest fallback
before an assessment exists. `current_risk` and `mean_risk` remain smoothed Φ
telemetry and can legitimately disagree with the headline.

### Margin

`margin` describes how much headroom you have before the nearest state-space edge. It is a small enum, not a number:

| `margin` | Meaning | What to do |
|----------|---------|------------|
| `settling` | Warmup — fewer than 3 check-ins, so there is not enough history to judge headroom yet (a crossed edge still reports `warning` / `critical` during warmup) | Keep checking in; a real margin appears after 3+ check-ins |
| `comfortable` | Clear of every edge that could be measured, by a healthy distance. Read `margin_scope` before treating it as "nothing is near" | Proceed normally |
| `tight` | Within the band around the nearest decision threshold | Be more careful with next steps; avoid increasing complexity |
| `warning` | An edge has just been crossed (less than 0.1 past the threshold) | Stop increasing complexity; reflect before the next step |
| `critical` | An edge is crossed deeply (0.1 or more past the threshold) | Halt the current approach; recover or escalate |

The actionable levels are `tight`, `warning`, and `critical` — each carries a companion `nearest_edge` field naming which boundary you are closest to: `risk`, `coherence`, or `void` from the margin computation, or `oscillation` when a CIRS `cirs_block` pause was driven by resonance. On `comfortable` and `settling`, `nearest_edge` is `null`.

`margin` is distance to a **decision threshold**, not a basin position. Two fields ride beside it and qualify what a `comfortable` reading actually covers:

| Field | Values | Meaning |
|---|---|---|
| `margin_scope` | `all_edges`, `measured_edges_only` | Whether every edge was judged, or only some of them. `GovernanceConfig.compute_proprioceptive_margin` sets it only on `comfortable`/`tight`, but several decision paths in `GovernanceConfig.make_decision` and `monitor_decision.make_decision` re-emit it with a default of `all_edges`, so an `all_edges` reading beside a `settling`/`warning`/`critical` margin is that default and not a finding that every edge was judged |
| `unmeasurable_edges` | list of edge names | The edges that had no band to judge against, so they were not assessed at all |

An edge is unmeasurable when it has no threshold band for this agent. Coherence is the usual case, and the gate is provenance, not history: the coherence edge is judged only when `coherence_role` is `behavioral_update_consistency` (`GovernanceConfig.COHERENCE_INTERPRETABLE_ROLE`), the history window carries that same role, and at least 10 samples exist. With the deployed `legacy_tanh_v` / `ode_control_feedback` producer the edge stays unmeasurable no matter how much history accumulates, so `comfortable` normally arrives as `margin_scope: measured_edges_only` with `unmeasurable_edges: ["coherence"]`. `comfortable` with `margin_scope: measured_edges_only` means "clear of the edges we could judge", not "nothing is near": read `unmeasurable_edges` for what was never assessed. Prefer the live values over assuming a fixed enum across runtime versions — `check_working_state(client_session_id=...)` is the source of truth.

Do not transfer this check-in margin into recovery eligibility. Recovery emits a
separate `recovery.margin.v2` view whose authoritative inputs are risk and
`void_active`; legacy coherence is explicitly listed as excluded diagnostic
context. Its enum is its own (`unknown` / `critical` / `tight` / `comfortable`,
with `nearest_edge` ∈ `void_active` / `risk_score` / `no_risk_authority`).

The plain-English `mirror` array in your check-in response already summarizes anything actionable (including a tight/warning/critical margin) — read that first. In `mirror` mode `margin`/`nearest_edge` are surfaced **only** when actionable; a `comfortable`/`settling` margin is steady-state and stays out of the response (the mirror's "No actionable signals — steady state" line covers it).

## Coherence

`coherence` is an overloaded compatibility field, not one universal instrument.
Interpret it only with the accompanying `coherence_source` and `coherence_role`.
The deployed `legacy_tanh_v` value is ODE control feedback: it feeds E, I, the
omitted-confidence estimator and the compatibility backstops, and it is not behavioral health evidence. The
dynamical-systems (ODE) model feeds later behavioral observations and final
structural checks. Behavioral assessment owns the main risk/verdict path after
the two-check-in prior window; this does not remove the ODE's influence on the
final action. For live interpretation read the behavioral assessment and policy
provenance.

| Source | Role | Valid interpretation |
|--------|------|----------------------|
| `legacy_tanh_v` | `ode_control_feedback` | Directional ODE controller activation. It is monotone in signed V, equals 0.5 at balance, and is **not** a symmetric health/balance score. |
| `manifold` | `eis_structural_measurement` | Grounded distance over E/I/S. It has a different distribution; legacy thresholds do not transfer. |
| `behavioral_assessment` | `behavioral_update_consistency` | HCK/update consistency carried in the behavioral assessment, distinct from the canonical compatibility scalar. |
| `grounded` | `eis_structural_measurement` | Same role as `manifold`; a grounded structural reading. |

- Full range is [0, 1], but range alone does not establish semantics.
- Untagged historical rows are `unknown_legacy` unless their producer can be reconstructed deterministically.
- Existing critical thresholds are compatibility gates, not validated quality grades. Do not recalibrate them merely to force alarm crossings.
- The check-in actuator still contains those backstops. Outside that migration
  boundary, recovery eligibility/margin, stuck recovery, dialectic triage,
  anomaly entropy, peer ranking, and automatic ground-truth fallback no longer
  treat legacy `C(V)` as authority.
- New dialectic conditions should not target legacy coherence. Stored
  unprovenanced `coherence_target` conditions retire without escalating policy.

## Calibration

The system tracks whether your stated confidence matches evidence. Over time this builds a calibration curve.

- Grounding comes from objective signals: test pass/fail, command exit codes, lint results, file operations. These feed calibration automatically via `auto_ground_truth.py` and the `outcome_event` hook. Human validation is not required for deterministic evidence.
- What that grounding is WORTH depends on who observed it. An outcome you submit yourself is graded `tool_observed` (evidence weight 0.65) at best, because the public path caps a caller attesting its own result — and 0.65 is exactly the calibration admission floor, so a self-described tool call is admitted at the boundary, not above it. Rows the server or an external signal observed can grade higher (0.85 / 1.00). The grade is not a score of your work; it is a statement about who checked. When a claim grades `claim_only`, the response's `corroboration_hint` names the reference keys the grader reads (they reach `self_report_with_refs`, below the calibration floor).
- While the binding ledger is retained, prediction-bound outcomes are database-idempotent per `(agent_id, prediction_id)`: an identical retry returns the first canonical outcome, while conflicting reuse is rejected. New eligible bound outcomes create durable calibration work in that same transaction. A recovery worker delivers each sample once per configured sink using receipts committed with projection state; transient failures leave work pending. File consumers must share the actual durable files and lock. Changing destination, backend or epoch does not silently reroute pending work. Older outcomes without receipts are not automatically backfilled, and unbound submissions retain best-effort, non-idempotent semantics. After both canonical outcome and binding expire, a new canonical submission may be established. The prediction registry is a cache carried in the monitor snapshot; durable binding authority lives in the database.
- Overconfidence is tracked and can lower Integrity / raise uncertainty through the check-in pipeline
- When an agent omits confidence, the deployed compatibility estimator still gives legacy `C(V_ODE)` 55% of its base weight. Responses expose this as `confidence_reliability.coherence_dependency=ode_control_feedback`; it is known causal debt, not independent confidence evidence. Do not reweight it without prospective outcome calibration because confidence history can feed later entropy penalties.
- That derived estimate stays internal: omitted confidence does not mint an agent tactical prediction or become an agent-reported calibration observation. Earlier explicit predictions remain available for their eventual outcomes. Simulation restores prediction bookkeeping and does not write trajectory calibration observations.

## Diagnostics

When the numbers look surprising, do not guess first. Use:

- `identity(client_session_id=...)` to verify who the runtime thinks you are, trusting it only when `identity_assurance.caller_proven` is true (with no arguments it mints a fresh identity instead of answering about yours)
- `health_check()` to verify the server and knowledge graph are healthy
- `check_working_state(client_session_id=...)` for the current interpreted
  state and risk provenance (without proof sent with the call, a read of your
  own state answers `unbound`); canonical `get_governance_metrics()` also returns the
  compatibility thresholds

## What NOT to Do

- **Do not treat coherence as a score to optimize** — interpret its producer and role
- **Do not ignore guide verdicts** — they are early warnings before pause/reject
- **Do not create duplicate discoveries** — always search the knowledge graph first
- **Do not check in after every trivial action** — it is noise, not signal
- **Do not leave high-severity findings as open forever** — resolve or archive them
