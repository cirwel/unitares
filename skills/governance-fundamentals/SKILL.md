---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, policy actions, coherence, calibration. Reference material for interpreting
  governance metrics as proprioceptive state estimation, not outcome judgment.
last_verified: "2026-11-17"
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
  - unitares/src/mcp_handlers/middleware/envelope_step.py
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
  - unitares/src/outcomes.py
  - unitares/skills/governance-fundamentals/references/eisv-and-calibration.md
  - unitares/skills/governance-fundamentals/references/verdicts-and-margin.md
---

# Governance Fundamentals

## What UNITARES Is

UNITARES is accountability infrastructure for long-running AI agents: a
self-hosted, single-operator federation kernel for agent identity, claims and
evidence, review, outcomes, and reconstruction. EISV runs synchronously on
check-ins as digital proprioception (blending and smoothing observations); see
the [computation contract](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md#role-runtime-and-evidence).

## EISV State Vector

| Dimension | Range | What the current computation records |
|-----------|-------|---------|
| **E** (Energy) | [0, 1] | Weighted observations: complexity divergence, recent outcome success, and available continuity and tool features (controller decision and coherence-level self-loops neutralized by default) |
| **I** (Information Integrity) | [0, 1] | Eligible per-agent calibration accuracy and non-echo outcome consistency (or uncalibrated base fallback when neither gate is met), plus available continuity and tool-variety features (controller coherence-trend and fleet-strategic self-loops neutralized by default) |
| **S** (Entropy) | [0, 1] | Drift, complexity divergence, and available continuity and tool features (controller regime-instability self-loop neutralized by default) |
| **V** (Valence) | [-1, 1] | Separately EMA-smoothed gap between raw E and I observations |

These are weighted indices with historical names, not thermodynamic quantities.
Deviation is information first, not a guilty verdict: in the default `observe`
posture, policy recommendations never block writes. Prefer live tool output.

## Live Readings, Defaults, and Stages

- **Self-loops neutralized by default**: `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`
  and `UNITARES_PER_AGENT_I_CALIBRATION=1` exclude legacy controller/coherence
  self-loops and lifetime fleet bins (`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0`
  or `UNITARES_PER_AGENT_I_CALIBRATION=0` restores their legacy terms).
- **Unread axes stay held** (`UNITARES_HOLD_UNREAD_AXES=1`): check
  `reading_coverage` (`measured`, `defaulted`, `v_rests_on_defaults`). A
  defaulted axis is a placeholder; after 10 unread check-ins it moves to
  `behavioral.assessment.unscored_axes`.
- **Stages**: check-ins 1–5 use the Φ prior; 6–27 (`behavioral.confidence >= 0.3`)
  use behavioral thresholds (else `insufficient_reading`); 28+ enable Welford
  z-score baselines.

For per-axis formulas, era flags (`SH1`/`HU1`/`SD1`), or `outcomes_in_e`, read
`references/eisv-and-calibration.md` (`skills(name="governance-fundamentals", reference="eisv-and-calibration.md")`).

## Basins

- **High basin**: E and I are high, S is low, and |V| is within its band.
- **Low basin**: inspect contributing readings and the policy record before interpreting.
- **Boundary**: transitioning between basins; sub-action is `guide` regardless of headline, and `basin` does **not** force `margin: tight`.

Use `check_working_state(client_session_id=..., verbosity="standard")`
(`get_governance_metrics()` canonically) as the source of truth for current
basin/mode labels; the default envelope omits them.

## Verdicts and Margin

Check-ins distinguish `policy_evaluation.action` (raw recommendation), top-level
`action` (effective operational directive), and `enforcement.applied` (whether a
lifecycle hold crossed the boundary). Never read `verdict.value` (a glossary
wrapper) as the action.

| Action | Sub-action | Meaning | What to do |
|--------|-----------|---------|------------|
| **proceed** | `approve` | Effective action is proceed; approval is this policy's classification | Continue working normally |
| **proceed** | `guide` | Policy supplies guidance | Read the guidance and assess its relevance to the work |
| **proceed** | `guide` with `recommended_action=pause` | Default observation posture: an inferred edge was crossed, but no hold was applied | Inspect if useful; continuing is neither permission nor a safety certification |
| **pause** | named pause with `enforcement.applied=false` | Enforcement posture returned the compatibility directive, but this path did not commit a lifecycle hold | Do not infer a write gate; inspect enforcement or recovery state |
| **pause** | `reject` or a named pause, with `enforcement.applied=true` | Explicit compatibility actuator applied a lifecycle hold | Stop and use the named recovery, optional dialectic review, or human input |

`margin` is distance to the nearest decision threshold (`nearest_edge` ∈ `risk`,
`coherence`, `void`, or `oscillation` when actionable; `null` on `comfortable`/`settling`):

| `margin` | Meaning | What to do |
|----------|---------|------------|
| `settling` | Warmup — fewer than 3 check-ins (a crossed edge still reports `warning`/`critical`) | Keep checking in |
| `comfortable` | Outside the tight band for every **measured** edge — check `margin_scope` and `unmeasurable_edges` | Proceed normally |
| `tight` | Within the band around `nearest_edge` | Avoid increasing complexity |
| `warning` | Less than 0.1 past `nearest_edge` | Stop increasing complexity; reflect |
| `critical` | 0.1 or more past `nearest_edge` | Halt current approach; recover or escalate |

`comfortable` with `margin_scope: measured_edges_only` means "clear of the edges
we could judge", not "nothing is near" (legacy `coherence` is normally in
`unmeasurable_edges`). Do not transfer check-in `margin` into recovery
eligibility (`recovery.margin.v2` is a separate view).

When `policy_basin` disagrees with `basin`, `risk_score_source` differs from
`current_risk`, `unmeasurable_edges` is non-empty, or you are interpreting
`recovery.margin.v2` or `mirror` mode filtering, read
`references/verdicts-and-margin.md`
(remote MCP: `skills(name="governance-fundamentals", reference="verdicts-and-margin.md")`).

## Coherence

`coherence` is an overloaded compatibility field, not one universal instrument.
Interpret it only with the accompanying `coherence_source` and `coherence_role`.
The deployed `legacy_tanh_v` value is ODE control feedback: it feeds the
omitted-confidence estimator and the compatibility backstops (its direct sensor self-loops into E and I are neutralized by default under `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`), and it is not behavioral health evidence. The
dynamical-systems (ODE) model still feeds final
structural checks. Behavioral assessment owns the main risk/verdict path once behavioral confidence reaches 0.3 (after the cold-start prior window); this does not remove the ODE's influence on the final action. For live interpretation read the behavioral assessment and policy
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

- Grounding comes from objective signals (test pass/fail, exit codes, lint, file
  ops) via `auto_ground_truth.py` and `outcome_event`, without requiring human
  validation for deterministic evidence.
- Self-submitted outcomes cap at `tool_observed` (evidence weight 0.65, the
  calibration admission floor); server/external observations grade higher
  (0.85 / 1.00). Overconfidence lowers Integrity / raises uncertainty.
- When an agent omits confidence, the deployed compatibility estimator still
  gives legacy `C(V_ODE)` 55% of its base weight, exposed as
  `confidence_reliability.coherence_dependency=ode_control_feedback` — known
  causal debt, not independent confidence evidence. Omitted confidence does not
  mint a tactical prediction.

When `record_result` returns `claim_only`, `corroboration_hint`, or
`PREDICTION_REUSE_CONFLICT`, or when inspecting calibration ledger delivery and
simulation rules, read `references/eisv-and-calibration.md`
(remote MCP: `skills(name="governance-fundamentals", reference="eisv-and-calibration.md")`).

## Diagnostics

When numbers look surprising, do not guess first:

- `identity(client_session_id=...)` — verify runtime identity; trust only when `identity_assurance.caller_proven` is true (never call with no arguments: it mints a fresh identity)
- `health_check()` — verify server and knowledge graph health
- `check_working_state(client_session_id=...)` — read interpreted state and risk provenance (without proof it answers `unbound`); canonical `get_governance_metrics()` also returns compatibility thresholds

## What NOT to Do

- **Do not treat coherence as a score to optimize** — interpret its producer and role
- **Do not ignore guide verdicts** — they are early warnings before pause/reject
- **Do not create duplicate discoveries** — always search the knowledge graph first
- **Do not check in after every trivial action** — it is noise, not signal
- **Do not leave high-severity findings as open forever** — resolve or archive them
