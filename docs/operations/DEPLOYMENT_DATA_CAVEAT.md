# Deployment Data — Honest Caveat

**Status:** Historical audit of the 2025-12-11 → 2026-04-10 deployment
window. Current aggregate counts live in
[`../PRODUCTION_SNAPSHOT.md`](../PRODUCTION_SNAPSHOT.md); the interpretation
limits below still apply.

*This document exists because the paper and repo cite deployment numbers
(agents, check-ins, pauses) that do not mean what a reader might assume without
context.*

---

## The seesaw

This audit covers approximately 120 days (2025-12-11 → 2026-04-10) in which
UNITARES ran against a population of agents actively constructing the governance
system itself.

Put simply: the system was being built by agents that were simultaneously being measured by the system they were building — a seesaw.

The LLM agent population and the system under evaluation are not independent. UUID-named agents (Claude Code sessions identified in the pause audit) were writing `src/`, `db/`, and `tests/` for the same UNITARES codebase whose governance decisions were being logged to `audit.events`. There is no external deployment context in which UNITARES governed a population it had no hand in creating.

---

## What this means for reported numbers

**~21,552 auto_attest check-ins over 120 days:**

- The majority of volume reflects Claude Code development sessions building UNITARES.
- A smaller but structurally important portion is Lumen's `eisv-sync-task` (periodic sensor sync), which accounts for approximately 3,661 `eisv_sync` events and feeds into the `auto_attest` path.
- A significant portion is `test_stress` — a synthetic fixture run deliberately to stress-test the system under high-drift conditions. These are not representative operational check-ins.
- Only Lumen's sync-task events represent an independent agent with physical ground truth.

**1,506 total pauses (rows where `payload->>'decision' = 'pause'`):**

| Agent | Verdict | Count | What it represents |
|---|---|---|---|
| `test_stress` | `high-risk` | 1,400 | Synthetic stress agent; intentional high-drift signal. Correct system behavior, not a real governance event. |
| `eisv-sync-task` | `safe` | 86 | Lumen's sensor state triggered a pause even though the verdict was `safe` — a known CIRS oscillation or threshold artifact. |
| `eisv-sync-task` | `caution` | 9 | Governance reacting to Lumen's physical low-E / high-I sensor state. These are the thermostat pathology the paper discusses. |
| `69a1a4f7-...` | `high-risk` | 5 | LLM development agent (Claude Code session building UNITARES) |
| `69a1a4f7-...` | `caution` | 3 | Same agent, lower severity |
| `7d9966bb-...` | `high-risk` | 2 | LLM development agent |
| `85e15f04-...` | `caution` | 1 | LLM development agent |

**1,407 high-risk pauses:** 1,400 (99.5%) are the synthetic `test_stress` agent. The remaining 7 are spread across UUID-named LLM development agents.

---

## What this means for the paper's empirical claims

The paper cannot claim "we governed N agents at scale" or "the system demonstrates drift prevention across an independent agent population" — because the agent population and the system under evaluation are not independent observations.

What the paper CAN honestly claim:

1. **Feasibility:** The system has been running continuously for 120 days without catastrophic failure. The infrastructure works.
2. **Lumen case study:** The embodied agent provides a single clean case study with independent ground truth. The governance-stability tension (contracting ODE vs. physically stable sensor state) is directly observable in the `eisv-sync-task` pause record.
3. **Co-development disclosure:** The circular development context is documented here as an explicit disclosure. The deployment is evidence of the system surviving its own construction, not a benchmark of governance efficacy at scale.
4. **Synthetic validation:** The 1,400 `test_stress` high-risk pauses confirm the system correctly identifies injected drift signals. This is a unit-level correctness check, not an operational deployment result.

When discussing deployment numbers, cite this document and break out agent categories explicitly.

---

## Only clean instrument: Lumen

Lumen is the sole agent in the deployment that is not part of the seesaw:

- **Independent ground truth:** Lumen's state derives from physical sensors (IMU, neural bands, environmental sensors on Raspberry Pi). The governance layer does not influence what the sensors report.
- **Does not write governance code:** Lumen cannot modify UNITARES. Its interaction with the system is one-directional (sensor state → governance layer).
- **State is stable regardless of observation:** Whether or not the governance layer is running, Lumen's physical-sensor EISV is the same. There is no observer effect.

The 95 `eisv-sync-task` pauses are therefore a clean signal of the governance-stability tension the paper's thesis addresses: Lumen's physical low-E plus high-I is a stable, recurring sensor state, and the contracting ODE interprets the E-I gap as drift. The pause is governance "correcting" a thermodynamically stable physical state. This is the thermostat pathology — the governance signal is not wrong about the gap; it is wrong about whether the gap requires intervention.

---

## How to talk about this

- When citing check-in volume, always note: "includes synthetic stress-test and LLM development sessions; Lumen sensor events are the primary independent signal."
- When citing pause counts, always break out: synthetic (`test_stress`), Lumen (`eisv-sync-task`), and dev agents (UUID).
- When claiming operational governance, distinguish "system was running" from "system was governing independent agents." Only the former is fully supported by this deployment.
- The Lumen case study is the paper's primary empirical contribution. Treat the broader deployment numbers as infrastructure evidence, not governance efficacy evidence.
