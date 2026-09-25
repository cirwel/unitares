# Unitares Architecture

**How agents check in, how state evolves, how verdicts are issued.**

Status: canonical prose summary. If this file and runtime code disagree, trust [dev/CANONICAL_SOURCES.md](dev/CANONICAL_SOURCES.md) and the referenced runtime files.

```
  Any AI Agent                                    Unitares Server
  (Claude Code, Codex,                            (port 8767)
   Hermes, any MCP client)
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

An agent calls `process_agent_update` (advertised to agents as `sync_state`) with:
- `response_text` — what it did; primary operational input
- `complexity` — optional reflective self-report [0, 1]
- `confidence` — optional reflective self-report [0, 1]

Identity is **not** auto-resumed across process boundaries. Per the v2 identity ontology, a fresh process-instance mints a fresh agent UUID; cross-process continuity is *declared* (via `parent_agent_id`) and verified, not silently inherited. The full model — performative vs descriptive vs inventive stances, layered continuity taxonomy, the substrate-earned identity pattern — is in [`ontology/identity.md`](ontology/identity.md).

At runtime, the reflective fields above (`complexity`, `confidence`) are not trusted in isolation. The dual-log layer compares them against server-derived operational signals, tool usage, continuity metrics, and other exogenous evidence when available.

### 2. EISV Evolution

**Primary system: Behavioral EISV** — EMA (exponential moving average)
observations from grounded behavioral signals. These signals are assembled from
operational log analysis, continuity metrics, tool usage, calibration history,
and outcome history; self-reports are one input, not the whole substrate. With
the current constants, check-ins 1–2 use the Φ cold-start prior, check-ins 3–24
use behavioral fixed thresholds, and check-in 25 enables per-agent Welford
z-score scoring against a 30-update target.

**Secondary system: ODE (diagnostic only)** — coupled differential equations run in parallel but do not drive verdicts. The ODE provides a dynamical-systems lens for analysis but behavioral verdicts override.

The grounding path lives in `src/dual_log/`, `src/behavioral_sensor.py`, `src/behavioral_state.py`, and `src/behavioral_assessment.py`. The ODE engine lives in `governance_core/` at the top level of this repo — pure Python, no separate install (it was a private compiled wheel, `unitares-core`, until it was folded back in 2026-04).

### 3. Ethical Drift

Four observable signals define a drift vector that feeds entropy:

| Signal | What it measures |
|--------|-----------------|
| Calibration deviation | Stated confidence vs actual outcomes |
| Complexity divergence | Self-reported complexity vs system estimate |
| Coherence deviation | How far coherence has moved from baseline |
| Stability deviation | Decision-pattern instability (`1 − decision_consistency`) |

No human oracle is needed for runtime drift estimation. Independent exogenous outcomes still matter for calibration and research validation.

### 4. Verdict

The decision engine (`src/monitor_decision.py`) returns one of two actions, `proceed` or `pause`; `guide` is a `sub_action` of `proceed` that the response surfaces as its own policy action:

| Policy action | Meaning | Agent action |
|---------|---------|-------------|
| `proceed` | State is healthy | Continue working |
| `guide` | `proceed` with guidance — slightly off track or at a basin boundary | Read guidance, adjust approach |
| `pause` | A hard stop: check-ins and new shared-memory entries are refused, not queued | Stop, read the reason, `self_recovery(action="check")` for self-recovery eligibility |

`reject` is no longer an action: it appears as a `sub_action` of `pause` on the risk-threshold path, and is accepted as a legacy input alias that normalizes to `pause`.

Responses include `margin` indicating proximity to basin boundaries: `comfortable` / `tight`, `warning` (a threshold just crossed, < 0.1 past), `critical` (≥ 0.1 past), or `settling` (fewer than 3 check-ins of history). See `config/governance_config.py`.

#### Edge-regime postures: oscillation and assessment failure

Two behaviors at the edges of the verdict path are deliberate and easy to miss when reading the code:

**Oscillation / churn near a threshold.** There is no deadband on the risk cut itself — a single check-in crossing a threshold can change the action. Stability instead comes from two layers: (1) the EISV inputs are EMA-smoothed before scoring (`src/behavioral_state.py`, per-dimension α ≈ 0.08–0.15), and (2) a dedicated always-on oscillation governor, **CIRS** (`src/cirs.py`, `src/monitor_decision.py`), watches the *decision* stream over a sliding window (default `window=8`, `flip_threshold=3`, `oi_threshold=3.0`). When decisions flip-flop, CIRS `soft_dampen` nudges `safe → caution`, and sustained resonance triggers a `hard_block` (`sub_action=cirs_block`, "governance is flip-flopping — wait for state to settle"). So churn is handled by *escalation to a settle-pause*, not by a hold-band — a deliberate trade (it prevents flip spam but biases conservative near the boundary; see the false-pause history behind the basin-health gate, issue #689).

**Assessment failure is fail-open.** If the assessment pipeline itself errors (DB/Redis outage, internal bug — distinct from the BEAM proxy, which fails open to in-process Python), the check-in handler returns an error response with **no `verdict`/`action` field** — it does not synthesize a `pause`. The agent proceeds. This is intentional for an advisory, non-guardrail system: fail-*closed* would let a transient infra blip mass-pause the whole fleet, and the anyio/asyncpg/Redis latency class (see *asyncpg, Redis and the anyio scheduler* under Database Architecture) makes that a live risk. The normal pause gates (CIRS / void / coherence / high-risk / low-basin) are unaffected; this covers only the case where no verdict could be computed at all. See the design comment at `src/mcp_handlers/core.py` (`process_agent_update` handler).

### 5. Calibration

The system tracks whether stated confidence matches outcomes. Ground truth comes from objective signals — test pass/fail, command exit codes, lint results. Over time this builds a calibration curve. Persistent overconfidence penalizes Information Integrity through entropy coupling.

## Transport Surfaces

Agents and operators interact through several bound services. All bind to `127.0.0.1` by default; LAN/tunnel exposure is opt-in via env vars (see [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) for the full surface).

| Service | Port | Endpoint | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard` (HTML), `/ws/eisv` (WebSocket event stream) | Primary agent surface — check-ins, queries, verdicts |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients |
| Wave 3A handlers | `8770` | — | BEAM-hosted handlers (`src/wave3a_routing.py`); routing table empty by default, so tools reach it only after an explicit cutover |
| Lease plane + governed-effect plane | `8788` | `/v1/lease/*`, `/v1/effects`, `/v1/dialectic/*`, `/v1/msg/*` (bearer-auth, fail-closed) | Elixir/OTP coordination layer for single-writer surfaces, governed effects, and agent messaging — runbook in [`operations/lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) |
| Agent orchestrator | `8789` | — | BEAM orchestrator used for dialectic reviewer dispatch (`src/mcp_handlers/dialectic/orchestrator_dispatch.py`) |
| Dialectic-live | `8790` | — | Phoenix/LiveView view of dialectic sessions |
| PostgreSQL + AGE | `5432` | `postgresql://…/governance` | Single source of truth (PG17 on the Homebrew deployment; the Docker image is PG18) |
| Redis | `6379` | `redis://…/0` | De-facto primary session/identity store — not optional; most live sessions exist only here. Durable identity/session state is moving to Postgres behind default-off mirror flags; Redis stays permanently for ephemeral and cross-process coordination (cache, locks, rate limits, pin TTLs) — operator topology decision 2026-06-28 in [`proposals/archive/redis-retirement-v0.md`](proposals/archive/redis-retirement-v0.md) |

The authoritative port registry is [`operations/DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md). Setting `UNITARES_UDS_SOCKET` adds an optional kernel-attested Unix-socket listener for residents (`src/uds_listener.py`).

## Recovery: Circuit Breaker + Dialectic

A pause refuses check-ins and new shared-memory entries (not queued); dialectic moves still work. A paused agent's risk is frozen at the reading that paused it, because it cannot write the check-in that would lower it, so most pauses end through a dialectic review, an operator (`operator_resume_agent`), or re-evaluation at auto-expiry (`src/mcp_handlers/support/pause_ttl.py`). The paths:

1. **Self-recovery** — `self_recovery(action="check")` reports eligibility. `quick` if risk ≤ 0.40 and no void is active; `review` with a reflection below risk 0.65. It lifts a pause only when the reading that paused the agent is already under those gates. Legacy `C(V)` is diagnostic context only
2. **Dialectic review** (`request_review`; thesis -> antithesis -> synthesis). The reviewer is one of:
   - **Orchestrated reviewer** — a separate process with its own identity (`agents/dialectic_reviewer/`; local, Codex, Claude or external backends). When `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW` is on (default off in code) it is the first choice; see [`proposals/active/orchestrated-dialectic-reviewer-v0.md`](proposals/active/orchestrated-dialectic-reviewer-v0.md)
   - **In-process synthetic reviewer** — the default, and the fallback when orchestration is on. Its verdict is binding (it approves only a RESUME synthesis whose antithesis does not dispute), but it runs in the caller's process under a synthetic reviewer id, so it is not an independent reviewer
   - **Peer agent** — another agent takes the antithesis role

See [dev/CIRCUIT_BREAKER_DIALECTIC.md](dev/CIRCUIT_BREAKER_DIALECTIC.md) for the full protocol.

## Knowledge Graph

Agents contribute discoveries to a shared store. **PostgreSQL FTS is the canonical retrieval backend** (`UNITARES_KNOWLEDGE_BACKEND=auto`, the default, resolves to `postgres` when `DB_BACKEND=postgres`); Apache AGE is an **optional graph backend** for queries that benefit from cypher-style traversal (`UNITARES_KNOWLEDGE_BACKEND=age`). The factory lives in [`src/knowledge_graph.py`](../src/knowledge_graph.py).

- Discoveries tagged with agent state, severity, and type
- Searchable across all agents and sessions; hybrid RRF (vector + FTS) requires the AGE backend — `KnowledgeGraphPostgres` exposes no `semantic_search`, and `UNITARES_ENABLE_HYBRID` defaults off
- Agents build on each other's findings — no re-discovery of known issues

## Database Architecture

```
+------------------------------+
|  PostgreSQL+AGE (port 5432)   |
|  core.*                      |     All agent state, audit,
|    identities, agents,       |     and knowledge lives here.
|    agent_state, sessions,    |     There is no SQLite.
|    session_bindings,         |
|    onboard_pins, calibration,|
|    dialectic_sessions,       |
|    dialectic_messages,       |
|    discovery_embeddings*     |     pgvector (per-model tables)
|  audit.*                     |
|    events, tool_usage,       |
|    outcome_events            |
|  knowledge.discoveries       |     relational KG record + FTS.
|  governance_graph (AGE)      |
|  metrics.series              |     Chronicler time series
|  lease_plane.*, effects.*,   |     BEAM coordination planes
|  coordination.*,             |
|  orchestration.*             |
|                              |
|  Redis (port 6379)           |     De-facto primary session store —
|  audit_log.jsonl (raw)       |     not optional; degraded local-only without it.
+------------------------------+
```

**Ownership is simple:** PostgreSQL+AGE is the single source of truth for all governance data.

### asyncpg, Redis and the anyio scheduler

The MCP SDK runs handlers inside an anyio task group. asyncpg and Redis run on
Python's asyncio. When a handler `await`s DB/Redis work, the two scheduler models
can interact in ways that hold connections across unrelated awaits and amplify
latency by orders of magnitude. Measured 2026-05-04 on the governance-MCP request
path: KG calls that complete in 21–71ms standalone ran at ~4,464ms in-handler — a
~60× amplification, with the floor sub-100ms. That figure is order-of-magnitude
only: other call paths gave 8–253×, and it was never pinned to a single handler. The Sentinel-loop call site
(`agents/sentinel/agent.py`) was mitigated to ">400 cycles, zero failures" via
PR #290, but that fix is one workaround at one site, not closure of the bug class.

**The attribution was withdrawn; the measurement was not.** The roadmap had
registered the test in advance: PR #350 (merged 2026-05-05) dropped `force=True`
from six observe sub-handlers, removing a 3221-await `load_metadata_async` loop
from the request path, and steady-state fell to 92–182ms. The V0.2 RESOLUTION of
[`proposals/active/beam-footprint-roadmap-v0.md`](proposals/active/beam-footprint-roadmap-v0.md)
records the verdict — "the 60× amplification floor was the 3221-await loop, not
anyio/asyncio coupling at the substrate layer". That surface is the one this
section cited until now as the class's most recent new variant: PR #348's
follow-up and PR #350 are the same change, so the 2026-05-04 measurement and the
"recent recurrence" were one event counted twice. Every floor raised since
resolved Python-side as well (#354, #360, #361, and #533's 104× p50 collapse),
and the roadmap's V0.4 RESOLUTION (2026-06-25) retired latency as a migration
decision gate on that basis.

What remains, stated at its real strength:

- **No measured instance is not absence.** The signature sub-type
  `coordination_failure.mcp_handler_timeout.tool_decorator` last fired
  2026-05-04. Five of the six wired sub-types have never fired, and the window
  behind that zero ran well below reference write load, so it is a coverage
  state rather than a clean bill — the four-state distinction CLAUDE.md requires
  before a zero is cited applies here too.
- **The incident record is three shipped mitigations, not three amplification
  events:** #84 (identity anyio-asyncio guard), #218 (ExecutorPool, deployed
  2026-04-27) and #290 (Sentinel forced-release poll, 2026-05-02), with S17
  (Redis-from-handler deadlock, 2026-04-26) as a fourth guard. The newest is
  2026-05-02; nothing has been added since.
- **The asyncpg half is mitigated; the Redis half is not.** ExecutorPool keeps
  asyncpg off the anyio loop. Async Redis clients remain unwrapped, so that path
  is untested rather than cleared, and the `asyncio.wait_for` guards below are
  the only protection on it.
- **The serialization ceiling is a separate and live claim**, and it belongs to
  the mitigation rather than to anyio: asyncpg work runs on one
  `ExecutorPool-loop` thread, and `execute_locked_update` serializes per agent
  through a shared mutex. Measured 2026-05-28 against a fresh-identity load
  generator: p50 51ms at four concurrent workers, 281ms at eight, 554ms at
  sixteen, zero errors.
- **The BEAM comparison is a design expectation, not a result.** Per-process
  scheduling with protocol-level connection checkout should not express this
  shape, but the comparison channel `measurement.beam_python_boundary.request`
  holds no rows, so nothing here measures it.

Whether to keep or retire the framing is an operator decision the roadmap has
deliberately left open: its 2026-05-28 amendment sets out two readings and
declines to choose between them. This section records what is measured and what
is not, and does not make that call.

**Current posture (PR #218, deployed 2026-04-27).** `get_db()` returns an
`ExecutorPool`-wrapped backend (`src/db/executor_pool.py`). asyncpg operations run
on a dedicated background thread with its own event loop, so the anyio task group
never sees an asyncpg await, and handlers use
`async with db.acquire() as conn: await conn.fetchval(...)` directly. Redis async
clients are not yet wrapped; the `asyncio.wait_for` timeouts in
`identity_step.py`, `persistence.py`, and `session.py` remain as the guard.

**Retired workaround patterns.** These predate ExecutorPool. They are retired for
new asyncpg handlers but remain in the codebase where they serve a purpose beyond
anyio isolation (Redis guards, sync blocking I/O, performance caches):

1. **Read cached data** populated by a background task — `health_check` reads
   `deep_health_probe_task`'s snapshot; sticky identity reads a cache pre-warmed by
   `transport_binding_cache_warmup`.
2. **`run_in_executor` with a sync client** — the `verify_agent_ownership` dispatch
   in `src/agent_loop_detection.py` pushes a synchronous DB-touching function to an
   executor thread so the anyio task group stays unblocked.
3. **`asyncio.wait_for` with a tight timeout** — degrade to a fallback on deadlock
   instead of hanging the pipeline: `deep_health_probe_task` in
   `src/background_tasks.py`, and `_load_binding_from_redis` in
   `src/mcp_handlers/middleware/identity_step.py` (500ms budget, returns `None` on
   timeout).

## Key Files

| File | Role |
|------|------|
| `governance_core.dynamics` | EISV differential equations |
| `governance_core.coherence` | Coherence function C(V, Theta) |
| `governance_core.adaptive_governor` | PID controller, oscillation detection |
| `config/governance_config.py` | Thresholds, margin computation |
| `src/mcp_server.py` | MCP server entry point |
| `src/mcp_handlers/core.py` | `process_agent_update` handler |
| `src/mcp_handlers/lifecycle/stuck.py` | Stuck detection, auto-recovery (`lifecycle/handlers.py` re-exports) |
| `src/mcp_handlers/lifecycle/self_recovery.py` | `self_recovery` dispatcher and quick path |
| `src/mcp_handlers/lifecycle/operations.py` | `self_recovery` review path (`handle_self_recovery_review`) |
| `src/mcp_handlers/dialectic/handlers.py` | Thesis/antithesis/synthesis |
| `src/calibration.py` | Confidence -> correctness mapping |
| `src/mcp_handlers/cirs/` | CIRS v2 protocol (7 message types) |

---

## Resident Agents

Several long-lived governance agents run alongside the server. They consume the same public contract as external agents (the [`unitares-sdk`](../agents/sdk/) package), and serve as both reference implementations and operational hygiene.

| Resident | Cadence | Role |
|---|---|---|
| **Vigil** | scheduled (launchd, ~30 min) | Janitorial — health checks, KG groundskeeping, test triggers |
| **Vigil hygiene** | weekly | Branch hygiene |
| **Sentinel** | continuous (`/ws/eisv`) | Fleet monitor — anomaly detection on the live event stream. The live slot is the BEAM Sentinel (`elixir/sentinel`); the Python Sentinel is the reference / rollback slot |
| **Watcher** | event-driven | Code-watcher — wired into Claude Code's PostToolUse hook, local-LLM pattern match |
| **Chronicler** | daily | Codebase, fleet and governance metrics → `metrics.series` |
| **Dialectic reviewer** | on dispatch | Orchestrated external reviewer (see Recovery above) |
| **Triage scribe** | on demand | Local-model anomaly summarizer |

Which residents a deployment runs is configuration (`UNITARES_RESIDENTS`, empty by default; see [`operations/resident-roster.md`](operations/resident-roster.md)). `agents/local_resident/` is the shared runner for local-model residents (currently the triage scribe). See [`agents/README.md`](../agents/README.md) for the reference implementations. The residents are reference patterns, **not** load-bearing governance internals — the public contract lives in `agents/sdk/`.

## Threat model and security posture

UNITARES has run continuously in production since November 2025 on a **single-operator fleet**. The threat model has been "internal fleet hygiene + honest agent identity," not "hostile external clients." Concretely:

- All services bind to `127.0.0.1` by default — public exposure is an operator decision (env-var gated)
- The lease plane fails closed if `LEASE_PLANE_BEARER_TOKEN` is unset
- Agent identity is bearer-token-based with intentional retention of the symmetric stack (asymmetric DPoP considered, shelved 2026-04-19 — see [`ontology/s1-continuity-token-retirement.md`](ontology/s1-continuity-token-retirement.md))
- Dashboard browser sign-in uses passkey/WebAuthn sessions (`src/dashboard_auth.py`), which MCP auth never consults. HTTP routes also accept the trusted-network bypass or `UNITARES_HTTP_API_TOKEN` in local posture, or an MCP bearer in strict posture (`src/http_routes/access.py`)

Multi-tenant or public-facing deployment will benefit from a harder auth posture than the current defaults. Vulnerability reports: [`SECURITY.md`](../.github/SECURITY.md).

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
- **[`proposals/`](proposals/)** — RFCs that don't (yet) belong in `ontology/`. [`proposals/active/README.md`](proposals/active/README.md) is the status index (Built / Active / Parked). Already built: the Plexus boundary over the surface lease plane ([`plexus-scope.md`](proposals/active/plexus-scope.md); [`surface-lease-plane-v0.md`](proposals/active/surface-lease-plane-v0.md) Phase A #305, `resident` enforcement #476), the agent message transport, and the orchestrated dialectic reviewer. Still active: the BEAM roadmap ([`beam-footprint-roadmap-v0.md`](proposals/active/beam-footprint-roadmap-v0.md) — Wave 3 signed GO-WITH-REDUCED-SCOPE, no implementation authorised yet). Most of the `wave-*` series is archived.
- **The paper** — [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)). v7 is in scoping; see [`ontology/paper-positioning.md`](ontology/paper-positioning.md).

If runtime code and this doc disagree, runtime wins. Disputes resolve against [`dev/CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md).
