# UNITARES User Manual

An end-to-end, task-ordered guide to **installing, running, integrating with, reading, and operating** the UNITARES governance MCP server. It is written for two readers at once — the **operator** who stands the server up and the **integrator** who points an agent at it — and it stitches the repo's thinner, single-topic docs into one walkthrough.

> This manual is a guided front door, not the canonical spec. Where it summarizes a deeper doc it links to it, and **if this manual and runtime code disagree, runtime code wins** (disputes resolve against [`../dev/CANONICAL_SOURCES.md`](../dev/CANONICAL_SOURCES.md)). For the one-paragraph pitch and the 60-second demo, start at the [repo README](../../README.md).

## What UNITARES is, in one breath

Runtime governance and online state estimation for fleets of autonomous AI agents. Each agent checks in while it works; UNITARES compares the current run to that agent's *own* baseline and returns a four-number state vector (EISV) plus one plain policy action — `proceed` / `guide` / `pause` / `reject` — so drift becomes visible to the agent while the output still looks fine. It runs **alongside** evals (pre-deploy) and guardrails (per-action), answering a third question: *is this agent still healthy as it works?*

## How to read this manual

| If you want to… | Read |
|---|---|
| Understand the idea and the vocabulary first | [1 · Overview & concepts](01-overview.md) |
| Get a server running on your machine | [2 · Installation](02-install.md) |
| Run, configure, and expose the server; open the dashboard | [3 · Running the server](03-running-the-server.md) |
| Wire an agent or MCP client into the check-in loop | [4 · Integrating agents](04-integrating-agents.md) |
| Interpret EISV, policy actions, coherence, drift, and the knowledge graph | [5 · Reading the signals](05-reading-the-signals.md) |
| Keep it healthy in production | [6 · Operating](06-operating.md) |
| Fix something that's broken | [7 · Troubleshooting & FAQ](07-troubleshooting.md) |

### Two fast paths

- **Operator, "just make it run":** [Try it in 60 seconds](../../README.md#try-it-in-60-seconds) (Docker) → [3 · Running the server](03-running-the-server.md). Bare-metal instead: [2 · Installation](02-install.md).
- **Integrator, "I have a server, wire my agent":** [4 · Integrating agents](04-integrating-agents.md) → [5 · Reading the signals](05-reading-the-signals.md).

## Chapters

1. **[Overview & concepts](01-overview.md)** — what it is, where it fits, the EISV / policy-action / coherence / drift vocabulary, and the honest scope.
2. **[Installation](02-install.md)** — Docker quickstart and the bare-metal (Postgres + AGE + pgvector) playbook.
3. **[Running the server](03-running-the-server.md)** — entry point, ports, transports, the dashboard, environment configuration, and exposing beyond loopback.
4. **[Integrating agents](04-integrating-agents.md)** — the check-in loop, identity rules, the full tool surface, policy-action handling, and the long-running-agent SDK.
5. **[Reading the signals](05-reading-the-signals.md)** — EISV computation, policy actions and margin, calibration, the knowledge graph, dialectic review, and how *not* to trust the numbers blindly.
6. **[Operating](06-operating.md)** — resident agents, security posture, database ownership, config tuning, and the operator runbook map.
7. **[Troubleshooting & FAQ](07-troubleshooting.md)** — common failures and the questions new users actually ask.

## Where the canonical docs live

This manual deliberately does not restate the deep references. Keep these open alongside it:

- [`../UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) — the canonical architecture and pipeline.
- [`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) — the exact formulas the running code computes.
- [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md) — who it's for, why an agent can't game it, what's unproven.
- [`../REVIEWER_GUIDE.md`](../REVIEWER_GUIDE.md) — the falsifiability harness you run yourself.
- [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md) — client wiring and remote-connector auth.
- [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md) — the zero-assumption bare-metal install.
- [`../operations/OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) — production operations.
- [`../README.md`](../README.md) — the full map of the `docs/` tree.
