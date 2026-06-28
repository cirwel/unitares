---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, policy actions, coherence, calibration. Reference material for interpreting
  governance metrics as proprioceptive state estimation, not outcome judgment.
last_verified: "2026-06-28"
freshness_days: 14
source_files:
  - unitares/config/governance_config.py
  - unitares/src/auto_ground_truth.py
  - unitares/src/governance_monitor.py
  - unitares/src/behavioral_state.py
  - unitares/src/behavioral_assessment.py
  - unitares/src/monitor_decision.py
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

- **E (Energy)** blends observable progress signals such as decision success, coherence, complexity calibration, and sometimes external task evidence.
- **I (Integrity)** tracks whether claims/confidence match observed results and whether coherence is holding.
- **S (Entropy / drift)** rises with drift norm, regime instability, and complexity divergence.
- **V (Valence)** is derived from the E-I imbalance. Positive means running hot (motion outruns integrity); negative means running careful (integrity outruns progress).

The headline math is proprioceptive residuals. In the live behavioral path,
warmup uses fixed universal thresholds; after warmup, residual-like state comes
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

## Verdicts

Governance issues a decision after each check-in. The response's `verdict` field wraps the decision **action**, which is binary — `proceed` or `pause` — qualified by a `sub_action`:

| Action | Sub-action | Meaning | What to do |
|--------|-----------|---------|------------|
| **proceed** | `approve` | State is healthy | Continue working normally |
| **proceed** | `guide` | Something is slightly off | Read the guidance text, adjust approach |
| **pause** | `reject` | Risk threshold reached | Stop current work, reflect; dialectic review or human input |
| **pause** | `void_pause`, `coherence_pause`, `basin_pause`, `risk_pause`, `cirs_block` | A specific subsystem tripped | Read the `reason`/`guidance` fields; consider dialectic review |

Separately, `metrics.verdict` may carry an internal UNITARES verdict such as `safe` / `caution` / `high-risk`. Read it as interpreted state/context, not as moral judgment. In current default posture, behavioral assessment drives live policy actions: fixed thresholds during warmup, Welford z-score residuals after warmup, with floors and gates always in force. Φ/ODE scoring is telemetry and research lens, not the control loop.

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

Coherence measures how well your state vector holds together. It is calculated from EISV/monitor signals — not from the content of your work. Think of it as structural health, not semantic quality.

- Full range is [0, 1]
- Critical threshold is available via `get_governance_metrics()` in the `thresholds` field — do not hardcode it
- Do not chase a number — check in honestly and let it track naturally
- Coherence reflects balance, not performance

## Calibration

The system tracks whether your stated confidence matches evidence. Over time this builds a calibration curve.

- Grounding comes from objective signals: test pass/fail, command exit codes, lint results, file operations. These feed calibration automatically via `auto_ground_truth.py` and the `outcome_event` hook. Human validation is not required for deterministic evidence.
- Overconfidence is tracked and can lower Integrity / raise uncertainty through the check-in pipeline

## Diagnostics

When the numbers look surprising, do not guess first. Use:

- `identity()` to verify who the runtime thinks you are
- `health_check()` to verify the server and knowledge graph are healthy
- `get_governance_metrics()` for the current live thresholds and interpreted state

## What NOT to Do

- **Do not game coherence** by reporting low complexity / high confidence on everything
- **Do not ignore guide verdicts** — they are early warnings before pause/reject
- **Do not create duplicate discoveries** — always search the knowledge graph first
- **Do not check in after every trivial action** — it is noise, not signal
- **Do not leave high-severity findings as open forever** — resolve or archive them
