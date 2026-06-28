# Unitares Architecture

**How agents check in, how state evolves, how verdicts are issued.**

Status: canonical prose summary. If this file and runtime code disagree, trust [dev/CANONICAL_SOURCES.md](dev/CANONICAL_SOURCES.md) and the referenced runtime files.

```
  Any AI Agent                                    Unitares Server
  (Cursor, Claude Code,                           (port 8767)
   Claude Desktop, CLI, ...)
  ============================                    ========================

  Do work                     HTTP POST /mcp/
  Report: what you did,      ------------------->
    complexity, confidence    process_agent_update   Behavioral EISV
                                                      +---------------------+
                                                      | grounded signals    |
                                                      |  (logs, tools,      |
                                                      |   calibration)      |
                                                      | -> EMA + z-score    |
                                                      |    vs own baseline  |
                                                      +----------+----------+
                                                                 |       ODE / free-energy
                                                            coherence    runs in parallel as a
                                                            risk_score   diagnostic lens — it
                                                            margin       does NOT drive verdicts
                                                                 |
                              <----------------------------------+
                              {"action": "proceed",
                               "margin": "comfortable",
                               "guidance": "..."}
```

The engine is the **behavioral** path: observable work signals scored against
the agent's own baseline. The ODE / free-energy formulation is a parallel
research lens, not the verdict authority — see [§2](#2-eisv-evolution).

## The Governance Pipeline

Every agent check-in flows through the same pipeline:

### 1. Check-in

An agent calls `process_agent_update` with:
- `response_text` — what it did; primary operational input
- `complexity` — optional reflective self-report [0, 1]
- `confidence` — optional reflective self-report [0, 1]

Identity is **not** auto-resumed across process boundaries. Per the v2 identity ontology, a fresh process-instance mints a fresh agent UUID; cross-process continuity is *declared* (via `parent_agent_id`) and verified, not silently inherited. The full model — performative vs descriptive vs inventive stances, layered continuity taxonomy, the substrate-earned identity pattern — is in [`ontology/identity.md`](ontology/identity.md).

At runtime, the reflective fields above (`complexity`, `confidence`) are not trusted in isolation. The dual-log layer compares them against server-derived operational signals, tool usage, continuity metrics, and other exogenous evidence when available.

### 2. EISV Evolution

**Primary system: Behavioral EISV** — EMA (exponential moving average) observations from grounded behavioral signals. These signals are assembled from operational log analysis, continuity metrics, tool usage, calibration history, and outcome history; self-reports are one input, not the whole substrate. After ~30 check-ins, the system builds per-agent Welford baselines and assesses agents by z-score deviation from their own operating point rather than universal thresholds.

**Secondary system: ODE (diagnostic only)** — coupled differential equations run in parallel but do not drive verdicts. The ODE provides a dynamical-systems lens for analysis but behavioral verdicts override.

The grounding path lives in `src/dual_log/`, `src/behavioral_sensor.py`, `src/behavioral_state.py`, and `src/behavioral_assessment.py`. The ODE engine lives in `governance_core` (compiled package, unitares-core).

### 3. Ethical Drift

Four observable signals define a drift vector that feeds entropy:

| Signal | What it measures |
|--------|-----------------|
| Calibration deviation | Stated confidence vs actual outcomes |
| Complexity divergence | Self-reported complexity vs system estimate |
| Coherence deviation | How far coherence has moved from baseline |
| Stability deviation | EISV variance over recent window |

No human oracle is needed for runtime drift estimation. Independent exogenous outcomes still matter for calibration and research validation.

### 4. Verdict

The server returns a governance decision:

| Verdict | Meaning | Agent action |
|---------|---------|-------------|
| `proceed` | State is healthy | Continue working |
| `guide` | Slightly off track | Read guidance, adjust approach |
| `pause` | Needs attention | Stop, reflect, consider dialectic review |
| `reject` | Significant concern | Requires dialectic review or human input |

Verdicts include `margin` (comfortable / tight / critical) indicating proximity to basin boundaries.

### 5. Calibration

The system tracks whether stated confidence matches outcomes. Ground truth comes from objective signals — test pass/fail, command exit codes, lint results. Over time this builds a calibration curve. Persistent overconfidence penalizes Information Integrity through entropy coupling.

## Transport Surfaces

Agents and operators interact through several bound services. All bind to `127.0.0.1` by default; LAN/tunnel exposure is opt-in via env vars (see [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) for the full surface).

| Service | Port | Endpoint | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard` (HTML) | Primary agent surface — check-ins, queries, verdicts |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients |
| Lease plane | `8788` | `/v1/lease/*` (bearer-auth, fail-closed) | Elixir/OTP coordination layer for single-writer surfaces — runbook in [`operations/lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) |
| PostgreSQL@17 + AGE | `5432` | `postgresql://…/governance` | Single source of truth |
| Redis | `6379` | `redis://…/0` | Session cache, optional |

## Recovery: Circuit Breaker + Dialectic

When an agent is paused, recovery follows a structured protocol:

1. **Self-recovery** — `self_recovery(action="quick")` if coherence > 0.60 and risk < 0.40
2. **LLM-assisted dialectic** — local LLM provides antithesis for single-agent reflection
3. **Peer dialectic** — another agent reviews (thesis -> antithesis -> synthesis)

See [dev/CIRCUIT_BREAKER_DIALECTIC.md](dev/CIRCUIT_BREAKER_DIALECTIC.md) for the full protocol.

## Knowledge Graph

Agents contribute discoveries to a shared store. **PostgreSQL FTS is the canonical retrieval backend** (`UNITARES_KNOWLEDGE_BACKEND=postgres`, default); Apache AGE is an **optional graph backend** for queries that benefit from cypher-style traversal (`UNITARES_KNOWLEDGE_BACKEND=age`). The factory lives in [`src/knowledge_graph.py`](../src/knowledge_graph.py).

- Discoveries tagged with agent state, severity, and type
- Searchable across all agents and sessions via hybrid RRF
- Agents build on each other's findings — no re-discovery of known issues

## Database Architecture

```
+------------------------------+
|  PostgreSQL+AGE (port 5432)   |
|  +- core.identities          |     All agent state, audit,
|  +- core.agent_state         |     and knowledge lives here.
|  +- audit.events             |
|  +- knowledge.discoveries    |     relational KG record + FTS.
|  +- discovery_embeddings     |     pgvector semantic search.
|  +- governance_graph (AGE)   |     There is no SQLite.
|  +- dialectic.*              |
|  +- core.calibration         |
|  +- core.tool_usage          |
|                              |
|  Redis (port 6379)           |     Session cache only.
|  audit_log.jsonl (raw)       |     Falls back gracefully without Redis.
+------------------------------+
```

**Ownership is simple:** PostgreSQL+AGE is the single source of truth for all governance data.

## Key Files

| File | Role |
|------|------|
| `governance_core.dynamics` | EISV differential equations (compiled) |
| `governance_core.coherence` | Coherence function C(V, Theta) (compiled) |
| `governance_core.adaptive_governor` | PID controller, oscillation detection (compiled) |
| `config/governance_config.py` | Thresholds, margin computation |
| `src/mcp_server.py` | MCP server entry point |
| `src/mcp_handlers/core.py` | `process_agent_update` handler |
| `src/mcp_handlers/lifecycle/handlers.py` | Stuck detection, auto-recovery |
| `src/mcp_handlers/dialectic/handlers.py` | Thesis/antithesis/synthesis |
| `src/calibration.py` | Confidence -> correctness mapping |
| `src/mcp_handlers/cirs/` | CIRS v2 protocol (7 message types) |

---

## Resident Agents

Several long-lived governance agents run alongside the server. They consume the same public contract as external agents (the [`unitares-sdk`](../agents/sdk/) package), and serve as both reference implementations and operational hygiene.

| Resident | Cadence | Role |
|---|---|---|
| **Vigil** | scheduled (launchd, ~30 min) | Janitorial — health checks, KG groundskeeping, test triggers |
| **Sentinel** | continuous (WebSocket) | Fleet monitor — anomaly detection on the live event stream |
| **Watcher** | event-driven | Code-watcher — wired into Claude Code's PostToolUse hook, local-LLM pattern match |
| **Chronicler** | daily | Longitudinal codebase metrics → `metrics.series` |
| **Steward** | in-process | EISV sync across substrates |

See [`agents/README.md`](../agents/README.md) for the reference implementations. The residents are reference patterns, **not** load-bearing governance internals — the public contract lives in `agents/sdk/`.

## Threat model and security posture

UNITARES has run continuously in production since November 2025 on a **single-operator fleet**. The threat model has been "internal fleet hygiene + honest agent identity," not "hostile external clients." Concretely:

- All services bind to `127.0.0.1` by default — public exposure is an operator decision (env-var gated)
- The lease plane fails closed if `LEASE_PLANE_BEARER_TOKEN` is unset
- Agent identity is bearer-token-based with intentional retention of the symmetric stack (asymmetric DPoP considered, shelved 2026-04-19 — see [`ontology/s1-continuity-token-retirement.md`](ontology/s1-continuity-token-retirement.md))
- The dashboard reads PostgreSQL directly with the same auth model as MCP

Multi-tenant or public-facing deployment will benefit from a harder auth posture than the current defaults. Vulnerability reports: [`SECURITY.md`](../SECURITY.md).

## Case Study: Lumen (Physical Sensor Agent)

One of the registered agents is [Lumen](https://github.com/cirwel/anima-mcp) — a Raspberry Pi 4 sensor-backed agent that checks in to Unitares every ~180 seconds (configurable via `ANIMA_GOVERNANCE_INTERVAL_SECONDS`).

What makes Lumen distinctive as an agent:

- **Physical sensors** (temperature, humidity, pressure, light) feed into labeled Anima dimensions (warmth, clarity, stability, presence), which are mapped to EISV for governance check-ins
- **Autonomous drawing** driven by a local EISV instance (DrawingEISV) that shares the same math but runs independently — coherence modulates how long Lumen draws and how picky it is about saving
- **Proprioceptive loop** — the light sensor reads Lumen's own LEDs, making clarity partly self-referential

Lumen demonstrates that Unitares can govern agents with very different architectures — from ephemeral CLI agents that check in once to persistent embodied systems with continuous sensor streams. The same EISV dynamics, the same verdicts, the same knowledge graph.

For Lumen's internal architecture (sensors, neural bands, DrawingEISV, LED pipeline, and creature-facing interface), see [anima-mcp](https://github.com/cirwel/anima-mcp).

---

## What's in flight

The system is in active development. Larger conceptual shifts and shipping RFCs live in:

- **[`ontology/`](ontology/)** — the versioned identity ontology and the research/system RFCs that evolve it. Start at [`ontology/README.md`](ontology/README.md).
- **[`proposals/`](proposals/)** — RFCs that don't (yet) belong in `ontology/`. The Plexus / lease-plane / BEAM-coordination work is here ([`plexus-scope.md`](proposals/plexus-scope.md), [`surface-lease-plane-v0.md`](proposals/surface-lease-plane-v0.md), [`beam-footprint-roadmap-v0.md`](proposals/beam-footprint-roadmap-v0.md), [`monitor-delegated-liveness-v0.md`](proposals/monitor-delegated-liveness-v0.md), and the `wave-*` series).
- **The paper** — [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)). v7 is in scoping; see [`ontology/paper-positioning.md`](ontology/paper-positioning.md).

If runtime code and this doc disagree, runtime wins. Disputes resolve against [`dev/CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md).
