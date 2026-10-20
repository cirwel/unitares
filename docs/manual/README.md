# UNITARES User Manual

An end-to-end, task-ordered guide to **installing, running, integrating with, reading, and operating** UNITARES. It is written for two readers at once — the **operator** who stands the server up and the **integrator** who points an agent at it — and it stitches the repo's thinner, single-topic docs into one walkthrough.

> This manual is a guided front door, not the canonical spec. Where it summarizes a deeper doc it links to it, and **if this manual and runtime code disagree, runtime code wins** (disputes resolve against [`../dev/CANONICAL_SOURCES.md`](../dev/CANONICAL_SOURCES.md)). For the one-paragraph pitch and the 60-second demo, start at the [repo README](../../README.md).

## What UNITARES is, in one breath

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them.

The record answers five questions about any piece of agent work: who said it,
what supports it, who challenged it, what happened, and what a successor can
recover. Process identity and lineage, durable findings, structured review and
typed outcomes are the mechanisms
([product definition](../PRODUCT_DEFINITION.md)).

Each check-in also returns a policy action, `proceed` or `pause`; a pause holds
governed writes until recovery, review, an operator or expiry lifts it. The
action is derived from EISV, a behavioral state estimate. EISV still ships in
the core path, and its move to an optional module follows a
[staged plan](../proposals/active/eisv-core-boundary-v0.md).

Core can refuse governed writes; the host remains responsible for actions
outside that boundary. MCP is the primary agent-facing interface, not the only
surface and not the agent runtime itself.

## How to read this manual

| If you want to… | Read |
|---|---|
| Understand the idea and the vocabulary first | [1 · Overview & concepts](01-overview.md) |
| Get a server running on your machine | [2 · Installation](02-install.md) |
| Run, configure, and expose the server; open the dashboard | [3 · Running the server](03-running-the-server.md) |
| Wire an agent or MCP client into the check-in loop | [4 · Integrating agents](04-integrating-agents.md) |
| Read findings, outcomes and the policy action; interpret EISV | [5 · Reading the record](05-reading-the-signals.md) |
| Keep it healthy in production | [6 · Operating](06-operating.md) |
| Fix something that's broken | [7 · Troubleshooting](07-troubleshooting.md) |

### Two fast paths

- **Operator, "just make it run":** [Install](../../README.md#install) (Docker) → [3 · Running the server](03-running-the-server.md). Bare-metal instead: [2 · Installation](02-install.md).
- **Integrator, "I have a server, wire my agent":** [4 · Integrating agents](04-integrating-agents.md) → [5 · Reading the record](05-reading-the-signals.md).

## Chapters

1. **[Overview & concepts](01-overview.md)** — what it is, where it fits, the record's vocabulary (identity, findings, review, outcomes), the policy action, and the EISV instrument.
2. **[Installation](02-install.md)** — Docker quickstart and the bare-metal (Postgres + AGE + pgvector) playbook.
3. **[Running the server](03-running-the-server.md)** — entry point, ports, transports, the dashboard, environment configuration, and exposing beyond loopback.
4. **[Integrating agents](04-integrating-agents.md)** — the check-in loop, identity rules, primary tools, policy-action handling, and links to the complete client references.
5. **[Reading the record](05-reading-the-signals.md)** — findings and shared memory, outcomes and their trust boundary, the policy action and recovery, then the EISV instrument and how *not* to trust its numbers blindly.
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
