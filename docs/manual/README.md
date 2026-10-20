# UNITARES User Manual

An end-to-end, task-ordered guide to **installing, running, integrating with, reading, and operating** the UNITARES MCP-native accountability and coordination layer. It is written for two readers at once — the **operator** who stands the server up and the **integrator** who points an agent at it — and it stitches the repo's thinner, single-topic docs into one walkthrough.

> This manual is a guided front door, not the canonical spec. Where it summarizes a deeper doc it links to it, and **if this manual and runtime code disagree, runtime code wins** (disputes resolve against [`../dev/CANONICAL_SOURCES.md`](../dev/CANONICAL_SOURCES.md)). For the one-paragraph pitch and the 60-second demo, start at the [repo README](../../README.md).

## What UNITARES is, in one breath

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them.

EISV is one instrument inside that record. Each agent checks in while it works
and receives a four-number state estimate (EISV) plus one policy action:
`action_summary.action` is `proceed` or `pause`, guidance arrives as `proceed`
with `sub_action="guide"`, and reject-severity decisions arrive as `pause` with
`sub_action="reject"`. The first two check-ins use a cold-start prior, the next
stage uses behavioral fixed thresholds, and self-relative scoring starts once
the agent has enough history. It runs **alongside** evals (pre-deploy) and
guardrails (per-action), answering a third question: *what state is this agent
in as it works?*

"Operating layer" describes the architecture, not universal host enforcement:
Core can refuse governed writes, while the host remains responsible for actions
outside that boundary. MCP is the primary agent-facing interface, not the only
surface and not the agent runtime itself.

## How to read this manual

| If you want to… | Read |
|---|---|
| Understand the idea and the vocabulary first | [1 · Overview & concepts](01-overview.md) |
| Get a server running on your machine | [2 · Installation](02-install.md) |
| Run, configure, and expose the server; open the dashboard | [3 · Running the server](03-running-the-server.md) |
| Wire an agent or MCP client into the check-in loop | [4 · Integrating agents](04-integrating-agents.md) |
| Interpret EISV, policy actions, coherence, drift, and the knowledge graph | [5 · Reading the signals](05-reading-the-signals.md) |
| Keep it healthy in production | [6 · Operating](06-operating.md) |
| Fix something that's broken | [7 · Troubleshooting](07-troubleshooting.md) |

### Two fast paths

- **Operator, "just make it run":** [Install](../../README.md#install) (Docker) → [3 · Running the server](03-running-the-server.md). Bare-metal instead: [2 · Installation](02-install.md).
- **Integrator, "I have a server, wire my agent":** [4 · Integrating agents](04-integrating-agents.md) → [5 · Reading the signals](05-reading-the-signals.md).

## Chapters

1. **[Overview & concepts](01-overview.md)** — what it is, where it fits, the EISV / policy-action / coherence / drift vocabulary, and the honest scope.
2. **[Installation](02-install.md)** — Docker quickstart and the bare-metal (Postgres + AGE + pgvector) playbook.
3. **[Running the server](03-running-the-server.md)** — entry point, ports, transports, the dashboard, environment configuration, and exposing beyond loopback.
4. **[Integrating agents](04-integrating-agents.md)** — the check-in loop, identity rules, primary tools, policy-action handling, and links to the complete client references.
5. **[Reading the signals](05-reading-the-signals.md)** — EISV computation, policy actions and margin, calibration, the knowledge graph, dialectic review, and how *not* to trust the numbers blindly.
6. **[Operating](06-operating.md)** — resident agents, security posture, database ownership, config tuning, and the operator runbook map.
7. **[Troubleshooting](07-troubleshooting.md)** — routes to the symptom-indexed troubleshooting guide and the install playbook.

## Where the canonical docs live

This manual deliberately does not restate the deep references. Keep these open alongside it:

- [`../PRODUCT_DEFINITION.md`](../PRODUCT_DEFINITION.md) — what the product is and the questions its record answers.
- [`../CAPABILITIES_AND_DEPLOYMENT.md`](../CAPABILITIES_AND_DEPLOYMENT.md) — what ships and how it deploys.
- [`../INTERFACE_CONTRACT.md`](../INTERFACE_CONTRACT.md) — the versioned agent-facing interface contract.
- [`../dev/TOOL_REFERENCE.md`](../dev/TOOL_REFERENCE.md) — every tool, action and parameter.
- [`../UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) — the canonical architecture and pipeline.
- [`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) — the exact formulas the running code computes.
- [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md) — who it's for, what anchors the signal, and what remains unproven.
- [`../REVIEWER_GUIDE.md`](../REVIEWER_GUIDE.md) — the falsifiability harness you run yourself.
- [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md) — client wiring and remote-connector auth.
- [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md) — the zero-assumption bare-metal install.
- [`../operations/OPERATOR_RUNBOOK.md`](../operations/OPERATOR_RUNBOOK.md) — production operations.
- [`../README.md`](../README.md) — the full map of the `docs/` tree.
