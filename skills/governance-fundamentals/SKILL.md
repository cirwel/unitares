---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, verdicts, coherence, calibration. Reference material for interpreting governance
  metrics and understanding the thermodynamic model.
last_verified: "2026-06-15"
freshness_days: 14
source_files:
  - unitares/config/governance_config.py
  - unitares/src/auto_ground_truth.py
  - unitares/src/governance_monitor.py
  - unitares/src/monitor_decision.py
  - unitares/src/mcp_handlers/core.py
---

# Governance Fundamentals

## What UNITARES Is

UNITARES provides digital proprioception for AI agents — awareness of your own state, your relationship to the system, and whether you are drifting. It tracks agent work through a thermodynamic model (energy, entropy, coherence) and maintains a shared knowledge graph across all agents.

## EISV State Vector

Every agent has four dimensions, updated through check-ins:

| Dimension | Range | Meaning |
|-----------|-------|---------|
| **E** (Energy) | [0, 1] | Productive capacity |
| **I** (Information Integrity) | [0, 1] | Signal fidelity |
| **S** (Entropy) | [0, 1] | Semantic uncertainty (lower is better) |
| **V** (Valence) | [-1, 1] | Accumulated E-I imbalance |

### How They Couple

- **E (Energy)**: Couples toward I (when I > E, energy rises). Dragged down by high entropy via E*S cross-coupling. High complexity affects E indirectly through S.
- **I (Information Integrity)**: Boosted by coherence C(V,Theta), reduced by entropy S. Has logistic self-regulation. Confidence and calibration affect I indirectly via the check-in pipeline (they drive S and ethical drift, which couple to I).
- **S (Entropy)**: Naturally decays (mu*S), rises with ethical drift and task complexity, reduced by coherence. The only dimension that directly responds to complexity.
- **V (Valence)**: Accumulated E-I imbalance. Positive when energy exceeds integrity (running hot), negative when integrity exceeds energy (running careful). Decays toward zero over time. Drives coherence via C(V,Theta).

These combine into a **coherence** score and **risk** score that determine governance decisions. Prefer live tool output over static range lore if the current runtime reports a narrower or more precise bound.

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

Separately, `metrics.verdict` carries the internal UNITARES verdict from phi scoring — `safe` / `caution` / `high-risk`. It drives the decision above; read it as context, not as the action itself.

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

Coherence measures how well your state vector holds together. It is calculated from the EISV values — not from the content of your work. Think of it as structural health, not semantic quality.

- Full range is [0, 1], clipped from thermodynamic C(V, Theta)
- Critical threshold is available via `get_governance_metrics()` in the `thresholds` field — do not hardcode it
- Do not chase a number — check in honestly and let it track naturally
- Coherence is derived from C(V, Theta) — it reflects balance, not performance

## Calibration

The system tracks whether your stated confidence matches outcomes. Over time this builds a calibration curve.

- Ground truth comes from objective signals: test pass/fail, command exit codes, lint results, file operations. These feed calibration automatically via `auto_ground_truth.py` and the `outcome_event` hook. Human validation is not required for deterministic outcomes.
- Overconfidence is tracked and penalizes Information Integrity through the entropy coupling

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
