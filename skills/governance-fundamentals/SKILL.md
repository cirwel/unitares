---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, policy actions, coherence, calibration. Reference material for interpreting
  governance metrics as proprioceptive state estimation, not outcome judgment.
last_verified: "2026-08-11"
freshness_days: 21
source_files:
  - unitares/config/governance_config.py
  - unitares/src/auto_ground_truth.py
  - unitares/src/governance_monitor.py
  - unitares/src/behavioral_state.py
  - unitares/src/behavioral_sensor.py
  - unitares/src/behavioral_assessment.py
  - unitares/src/monitor_decision.py
  - unitares/src/coherence_provenance.py
  - unitares/src/confidence.py
  - unitares/src/mcp_handlers/core.py
---

# Governance Fundamentals

## What UNITARES Is

UNITARES provides digital proprioception for AI agents — awareness of your own state, your relationship to the system, and whether you are drifting. The live path is behavioral state estimation: observable work signals become EISV readings, smoothed over time and compared with the agent's own trajectory once a baseline exists. The thermodynamic / ODE model remains useful as a research lens and telemetry; do not present it as cold-start authority or live verdict authority.

## EISV State Vector

Every agent has four dimensions, updated through check-ins:

| Dimension | Range | Meaning |
|-----------|-------|---------|
| **E** (Energy) | [0, 1] | Productive capacity |
| **I** (Information Integrity) | [0, 1] | Claims matching results / calibration |
| **S** (Entropy) | [0, 1] | Drift / instability from normal (lower is usually steadier) |
| **V** (Valence) | [-1, 1] | EMA-smoothed E-I imbalance |

### How the Live Path Reads Them

- **E (Energy)** currently blends decision success, complexity calibration, sometimes external task evidence, and a legacy coherence-level term sourced from `legacy_tanh_v` ODE control feedback. That last input is compatibility debt, not behavioral health evidence.
- **I (Integrity)** currently blends calibration/outcome consistency with the trend of that same legacy controller scalar. Read it as a deployed heuristic, not a pure claims-match-results measurement.
- **S (Entropy / drift)** rises with drift norm, regime instability, and complexity divergence.
- **V (Valence)** is derived from the E-I imbalance. Positive means running hot (motion outruns integrity); negative means running careful (integrity outruns progress).

The headline math is proprioceptive residuals. In the live behavioral path,
warmup verdicts fall back to a mostly server-derived cold-start prior (the behavioral track scores against fixed universal thresholds); after warmup, residual-like state comes
from self-relative z-score deviation against the agent's own Welford baseline,
with absolute safety floors and basin-health gates always in force.

Roadmap target semantics for richer cold-start grounding are:

```text
measurement_t = EISV_t
reference_t   = blend(agent_baseline_t, class_anchor; w(grounding))
residual_t    = measurement_t - reference_t
```

Do not present the class-anchor blend as deployed unless live code exposes it.
Deviation is information first, not a guilty verdict. Policy can map persistent or
high-margin residuals to `guide`, `pause`, or `reject`, but EISV itself is
measurement/diagnosis, not prosecution.

Prefer live tool output over static range lore if the current runtime reports a narrower or more precise bound.

## Basins

Your state sits in a basin — a region of the EISV space:

- **High basin**: Healthy. E and I are high, S and V are low. Normal operating range.
- **Low basin**: Degraded. May need recovery or intervention.
- **Boundary**: Transitioning between basins. Extra attention from governance. Verdicts may carry `margin: tight`.

Use `get_governance_metrics()` as the source of truth for the current basin/mode labels rather than assuming they are constant across runtime versions.

When a response includes `policy_evaluation.inputs.basin`, read it as the
decision-time policy basin. Agent-facing state fields can be sourced from the
primary EISV path for that response, so newer responses also include
`policy_basin`, `policy_basin_source`, and `primary_eisv_source` to make that
measurement distinction explicit.

## Verdicts

Governance issues a decision after each check-in. The response's `verdict` field wraps the decision **action**, which is binary — `proceed` or `pause` — qualified by a `sub_action`:

| Action | Sub-action | Meaning | What to do |
|--------|-----------|---------|------------|
| **proceed** | `approve` | State is healthy | Continue working normally |
| **proceed** | `guide` | Something is slightly off | Read the guidance text, adjust approach |
| **pause** | `reject` | Risk threshold reached | Stop current work, reflect; dialectic review or human input |
| **pause** | `void_pause`, `coherence_pause`, `basin_pause`, `risk_pause`, `cirs_block` | A specific subsystem tripped | Read the `reason`/`guidance` fields; consider dialectic review |

Separately, `metrics.verdict` may carry an internal UNITARES verdict such as `safe` / `caution` / `high-risk`. Read it as interpreted state/context, not as moral judgment. In current default posture, behavioral assessment drives the main risk/verdict path: fixed thresholds during warmup, Welford z-score residuals after warmup, with floors and basin gates always in force. Legacy coherence compatibility gates still exist while their replacement is shadow-calibrated; their presence does not make the ODE scalar behavioral evidence. Φ/ODE scoring is telemetry and research lens, not the primary control loop.

### Margin

`margin` describes how much headroom you have before the nearest state-space edge. It is a small enum, not a number:

| `margin` | Meaning | What to do |
|----------|---------|------------|
| `settling` | Warmup — fewer than 3 check-ins, so there is not enough history to judge headroom yet | Keep checking in; a real margin appears after 3+ check-ins |
| `comfortable` | Clear of every edge by a healthy distance | Proceed normally |
| `tight` | Within the edge threshold of the nearest boundary (or in the boundary basin) | Be more careful with next steps; avoid increasing complexity |
| `warning` | An edge has just been crossed (less than 0.1 past the threshold) | Stop increasing complexity; reflect before the next step |
| `critical` | An edge is crossed deeply (0.1 or more past the threshold) | Halt the current approach; recover or escalate |

The actionable levels are `tight`, `warning`, and `critical` — each carries a companion `nearest_edge` field naming which boundary you are closest to (`risk`, `coherence`, or `void`). On `comfortable` and `settling`, `nearest_edge` is `null` (there is no edge to warn about). Prefer the live `margin`/`nearest_edge` values over assuming a fixed enum across runtime versions — `get_governance_metrics()` is the source of truth.

The plain-English `mirror` array in your check-in response already summarizes anything actionable (including a tight/warning/critical margin) — read that first. In `mirror` mode `margin`/`nearest_edge` are surfaced **only** when actionable; a `comfortable`/`settling` margin is steady-state and stays out of the response (the mirror's "No actionable signals — steady state" line covers it).

## Coherence

`coherence` is an overloaded compatibility field, not one universal instrument.
Interpret it only with the accompanying `coherence_source` and `coherence_role`:

| Source | Role | Valid interpretation |
|--------|------|----------------------|
| `legacy_tanh_v` | `ode_control_feedback` | Directional ODE controller activation. It is monotone in signed V, equals 0.5 at balance, and is **not** a symmetric health/balance score. |
| `manifold` | `eis_structural_measurement` | Grounded distance over E/I/S. It has a different distribution; legacy thresholds do not transfer. |
| `behavioral_assessment` | `behavioral_update_consistency` | HCK/update consistency carried in the behavioral assessment, distinct from the canonical compatibility scalar. |

- Full range is [0, 1], but range alone does not establish semantics.
- Untagged historical rows are `unknown_legacy` unless their producer can be reconstructed deterministically.
- Existing critical thresholds are compatibility gates, not validated quality grades. Do not recalibrate them merely to force alarm crossings.
- Prefer the behavioral assessment and policy provenance for live interpretation; use legacy ODE values as telemetry/research context.

## Calibration

The system tracks whether your stated confidence matches evidence. Over time this builds a calibration curve.

- Grounding comes from objective signals: test pass/fail, command exit codes, lint results, file operations. These feed calibration automatically via `auto_ground_truth.py` and the `outcome_event` hook. Human validation is not required for deterministic evidence.
- Overconfidence is tracked and can lower Integrity / raise uncertainty through the check-in pipeline
- When an agent omits confidence, the deployed compatibility estimator still gives legacy `C(V_ODE)` 55% of its base weight. Responses expose this as `confidence_reliability.coherence_dependency=ode_control_feedback`; it is known causal debt, not independent confidence evidence. Do not reweight it without prospective outcome calibration because confidence history can feed later entropy penalties.

## Diagnostics

When the numbers look surprising, do not guess first. Use:

- `identity()` to verify who the runtime thinks you are
- `health_check()` to verify the server and knowledge graph are healthy
- `get_governance_metrics()` for the current live thresholds and interpreted state

## What NOT to Do

- **Do not treat coherence as a score to optimize** — interpret its producer and role
- **Do not ignore guide verdicts** — they are early warnings before pause/reject
- **Do not create duplicate discoveries** — always search the knowledge graph first
- **Do not check in after every trivial action** — it is noise, not signal
- **Do not leave high-severity findings as open forever** — resolve or archive them
