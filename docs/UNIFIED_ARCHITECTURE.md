# Unitares Architecture

**How the record is built: who did the work, what supports it, who challenged it, what happened, and what a successor can recover.**

Status: canonical prose summary. If this file and runtime code disagree, trust [dev/CANONICAL_SOURCES.md](dev/CANONICAL_SOURCES.md) and the referenced runtime files.

## Summary

**UNITARES is a self-hosted, single-operator federation kernel for agent
identity, claims and evidence, review, outcomes, and reconstruction.** Agents
carry findings, corrections, and review decisions across sessions and vendors;
a successor or operator can retrieve the record and inspect its sources.

“Federation” here means independent runtimes sharing one operator-controlled
server and authority domain. Each runtime keeps its own models, tools, and
execution loop, and communicates with peers through its own transport or A2A.
Nothing in UNITARES replicates state across independent servers.

The [product definition](PRODUCT_DEFINITION.md) owns that framing and the five
questions the record answers. This document maps them onto the system:

| Question | Layer |
|---|---|
| Who said it? | [Identity and lineage](#identity-and-lineage) |
| What supports it? | [Findings and the knowledge graph](#knowledge-graph) |
| Who challenged it? | [Review: dialectic and recovery](#review-dialectic-and-recovery) |
| What happened? | [Outcomes](#outcomes) |
| What can a successor recover? | Reads across those layers ([reconstruction](CAPABILITIES_AND_DEPLOYMENT.md#start-with-the-core-workflow)) |

EISV is one instrument inside the kernel, not its definition: each check-in
returns a behavioral state estimate and a policy action. The operator adopted
moving EISV out of the core to an optional module on 2026-10-10
([`proposals/active/eisv-core-boundary-v0.md`](proposals/active/eisv-core-boundary-v0.md)).
This settles the direction, not implementation authorization: the staged gates
and standing governed-review conditions still apply, and the estimator still
derives today's check-in policy action. [EISV_COMPUTATION.md](EISV_COMPUTATION.md)
owns the instrument.

## Layered architecture

The system as a set of layers, top-down. Each layer below states what it is,
its key modules, and its **honest current maturity** (not a roadmap
aspiration). Where a layer has a known limit, it is named here, not buried.

UNITARES Core does not own the agent execution loop. Agent harnesses, custom
clients, and reference residents sit above the public interfaces and remain
independent userlands. MCP is the primary agent-facing contract, while REST,
the SDK, adapters, and the dashboard expose the same Core. The general-purpose
**UNITARES Resident** belongs in that userland layer and must use those public
contracts; its separate repository ships an early skeleton, and it is not a
deployed Core component.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ USERLANDS       agent harnesses · custom clients · reference residents       │  run agents
├──────────────────────────────────────────────────────────────────────────────┤
│ INTERFACES      MCP /mcp/ · REST /v1 · Dashboard · SDK · governance plugin   │  reach Core
├──────────────────────────────────────────────────────────────────────────────┤
│ CORE: THE RECORD                                                             │
│   IDENTITY & LINEAGE   per-instance UUID · lineage DAG · proof tiers         │  who did what
│                        — anchors every write and verdict                     │
│   FINDINGS & KG        shared, provenance-tagged cross-agent memory          │  what supports it
│   REVIEW (DIALECTIC)   thesis / antithesis / synthesis, recorded positions   │  who challenged it
│   OUTCOMES             typed outcome events with provenance                  │  what happened
│   POLICY & EISV        state estimate → risk → action (proceed/pause         │  one instrument,
│                        + sub_action); moving to an optional module           │  not the record
├──────────────────────────────────────────────────────────────────────────────┤
│ COORDINATION    BEAM lease plane · governed effects · agent messaging        │  single-writer
├──────────────────────────────────────────────────────────────────────────────┤
│ STORAGE         one Postgres (relational + Apache AGE + pgvector) · Redis    │  durable truth
└──────────────────────────────────────────────────────────────────────────────┘
```

### Maturity at a glance

| Layer | Status | One-line honest read |
|---|---|---|
| Identity & lineage | **Mature** | Load-bearing; strict write gate live; anchors everything |
| Findings & knowledge graph | **Live** | Strong provenance + cross-agent linking; serving-path eval fixed, current score not yet on record; AGE live-but-advisory |
| Review (dialectic) | **In-flight** | Independent reviewer serving live where the orchestrated-review flag is on (default off); reviewer-quality is the open frontier |
| Outcomes | **Deployed** | Outcome claims are recorded with provenance; independence depends on the producer |
| Policy & EISV | **Deployed** | Runtime observations and controller history → smoothed indices → assessment and policy |
| Coordination (BEAM) | **Partial** | Advisory-first; Wave 3a read-only cutovers live; Wave 3 committed, not started |
| Reference residents · Storage · Interfaces | **Live / Mature** | Operational; UNITARES Resident has a separate early skeleton, not yet a usable agent product |

Evidence for particular diagnostic or decision benefits is tracked separately
in the [claim ledger](EVIDENCE_AND_LIMITS.md#current-claim-status).

## Userlands and long-lived agents

UNITARES governs any agent that checks in. It does not come with a fleet, and it
needs no resident agents to run. What the architecture provides for agents that
live longer than one session is a set of capabilities, not a roster:

- **Persistent identity.** A long-lived agent keeps a stable identity across restarts through the substrate-identity pattern (`agents/sdk/`, `_substrate.py`; see [`ontology/identity.md`](ontology/identity.md)). Ordinary sessions mint fresh identities instead.
- **Capability tags and declared properties.** Behaviour is decided by tags such as `persistent`, `embodied` and `ephemeral`, by a `cadence.*` tag for the expected check-in interval, and by what an agent sends. An agent that publishes a physical sensor reading is judged against it rather than the behavioural sensor. Two label-keyed overlays, for check-in intervals and dashboard silence thresholds, remain as back-compat fallbacks. They are empty by default and are meant to shrink to nothing. The resident-progress manifest (`UNITARES_RESIDENT_PROGRESS_MANIFEST`) is different: it is the progress probe's own opt-in configuration, keyed by label and empty by default.
- **An optional named roster.** `UNITARES_RESIDENTS` is **empty by default**. Naming an agent there does two things:
  - it gives the agent its own calibration class, which is a statistical partition;
  - it is the **only** sanctioned way an identity acquires the privileged `persistent` and `autonomous` tags, which an agent cannot grant itself. These tags exempt it from orphan archival and loop detection.

  A long-lived agent left off the roster onboards untagged and can be archived. See [`operations/resident-roster.md`](operations/resident-roster.md#non-obvious-consequence-privileged-tags).
- **The public contract.** Long-lived agents use the same MCP surface and the [`unitares-sdk`](../agents/sdk/) as any external agent. Nothing in the server depends on a particular one existing.

`agents/` ships **reference** long-lived agents: a janitor, a fleet monitor, a code watcher, a metrics chronicler and a local-model triage scribe. They are examples of the pattern and operational tools for the deployment that runs them. They are specialized, optional governed clients, not Core internals or a general agent framework, and are **live** in the reference deployment. They are outside the shipped server package and are not governance internals; see [`agents/README.md`](../agents/README.md). The orchestrated dialectic reviewer also lives in `agents/`, but it is part of an optional server feature and is described under [Review](#review-dialectic-and-recovery) below.

The range of agents this covers runs from an ephemeral CLI session that checks in once to a persistent embodied system. One deployment's example of the latter is a Raspberry Pi agent whose physical sensors map onto EISV ([anima-mcp](https://github.com/cirwel/anima-mcp)). The same record, EISV dynamics, policy actions and knowledge graph apply to both.

## Interfaces and transport surfaces

Agents and humans reach Core through MCP `/mcp/`, REST `/v1/tools/call`, the
dashboard `/dashboard`, the SDK, the governance plugin (Claude Code/Codex
hooks), and host adapters. All services bind to `127.0.0.1` by default;
LAN/tunnel exposure is opt-in via env vars (see [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) for the full surface).

| Service | Port | Endpoint | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard` (HTML), `/ws/eisv` (WebSocket event stream) | Primary agent surface — identity, findings, review, outcomes, check-ins |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients |
| Wave 3A handlers | `8770` | — | BEAM-hosted handlers (`src/wave3a_routing.py`); routing table empty by default, so tools reach it only after an explicit cutover |
| Lease plane + governed-effect plane | `8788` | `/v1/lease/*`, `/v1/effects`, `/v1/dialectic/*`, `/v1/msg/*` (bearer-auth, fail-closed) | Elixir/OTP coordination layer for single-writer surfaces, governed effects, and agent messaging — runbook in [`operations/lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) |
| Agent orchestrator | `8789` | — | BEAM orchestrator used for dialectic reviewer dispatch (`src/mcp_handlers/dialectic/orchestrator_dispatch.py`) |
| Dialectic-live | `8790` | — | Phoenix/LiveView view of dialectic sessions |
| PostgreSQL + AGE | `5432` | `postgresql://…/governance` | Single source of truth (PG17 on the Homebrew deployment; the Docker image is PG18) |
| Redis | `6379` | `redis://…/0` | De-facto primary session/identity store — not optional; most live sessions exist only here. Durable identity/session state is moving to Postgres behind default-off mirror flags; Redis stays permanently for ephemeral and cross-process coordination (cache, locks, rate limits, pin TTLs) — operator topology decision 2026-06-28 in [`proposals/archive/redis-retirement-v0.md`](proposals/archive/redis-retirement-v0.md) |

The authoritative port registry is [`operations/DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md). Setting `UNITARES_UDS_SOCKET` adds an optional kernel-attested Unix-socket listener for residents (`src/uds_listener.py`). The [interface contract](INTERFACE_CONTRACT.md) defines the transport-neutral capability names.

**Security defaults.** The deployment defaults assume a single trusted operator network:

- All services bind to `127.0.0.1` by default — public exposure is an operator decision (env-var gated)
- The lease plane fails closed if `LEASE_PLANE_BEARER_TOKEN` is unset
- Agent identity is bearer-token-based with intentional retention of the symmetric stack (asymmetric DPoP considered, shelved 2026-04-19 — see [`ontology/s1-continuity-token-retirement.md`](ontology/s1-continuity-token-retirement.md))
- Dashboard browser sign-in uses passkey/WebAuthn sessions (`src/dashboard_auth.py`), which MCP auth never consults. HTTP routes also accept the trusted-network bypass or `UNITARES_HTTP_API_TOKEN` in local posture, or an MCP bearer in strict posture (`src/http_routes/access.py`)

## Identity and lineage

Every action in the system attributes to a **per-instance identity**: a registry UUID minted on a
fresh process (`onboard(force_new=true)`), distinct from its public handle and its cosmetic label
(the s22 identity ontology — *identity is the UUID; the name is social*). Identity carries a
**proof tier** (`weak` → `strong`; ordinary continuation echoes the returned
`client_session_id`, while a short-lived `continuity_token` is reserved for an explicit
same-live-process rebind), a **lineage DAG**
(`parent_agent_id` / `spawn_reason`, declared not inferred), and a **strict write gate** — reads may
be anonymous, writes must be accountable.

Identity is **not** auto-resumed across process boundaries. Per the v2 identity ontology, a fresh process-instance mints a fresh agent UUID; cross-process continuity is *declared* (via `parent_agent_id`) and verified, not silently inherited. The full model — performative vs descriptive vs inventive stances, layered continuity taxonomy, the substrate-earned identity pattern — is in [`ontology/identity.md`](ontology/identity.md).

- **Modules:** `src/mcp_handlers/identity/`, `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/schemas/identity.py`, `core.identities`.
- **Invariants (structural, do not re-introduce):** never auto-`force_new`; no lookup-by-label; first MCP call is the sole identity source; co-location is not lineage.
- **Maturity: MATURE / load-bearing.** Strict identity is enforced in production. This layer **anchors the KG** (with the AGE knowledge backend, every discovery has an `Agent` vertex + `AUTHORED` edge) and every verdict (state attributes to a UUID).

## Knowledge Graph

Agents contribute discoveries to a shared store. **The relational rows are canonical.** The Docker quickstart and `.env.example` set `UNITARES_KNOWLEDGE_BACKEND=age`, which also writes the graph over those rows in Apache AGE; unset, a native install resolves to `postgres` (`auto` with `DB_BACKEND=postgres`), which serves the rows and full-text search without the graph. The AGE *extension* is required either way — lineage and other graph reads use it through `graph_query`. The factory lives in [`src/knowledge_graph.py`](../src/knowledge_graph.py).

- Discoveries tagged with agent state, severity, and type
- Searchable across all agents and sessions; hybrid RRF (vector + FTS) requires the AGE backend — `KnowledgeGraphPostgres` exposes no `semantic_search`, and `UNITARES_ENABLE_HYBRID` defaults off
- Agents build on each other's findings — no re-discovery of known issues

What one agent learns, the fleet keeps. Findings carry claims, corrections and
provenance; **every value is provenance-tagged** (`measured` / `derived` /
`prior`) so a stored memory never reads as more certain than it is. With the
AGE knowledge backend, one Postgres database holds **three representations** of
each discovery:

- **relational** `knowledge.discoveries` — the canonical record (+ FTS).
- **pgvector** `core.discovery_embeddings_bge_m3` — semantic search.
- **AGE property graph** `governance_graph` — `Agent`/`Discovery`/`Tag` vertices; `AUTHORED`/`RELATED_TO`/`SUPERSEDES`/`SPAWNED`/`TAGGED` edges — the live relationship layer (supersession chains, lineage, relatedness).

Hybrid retrieval, where enabled, is RRF-fused (vector + FTS) and **honestly
self-reports low confidence** when a query matches only semantically. Write
discipline: search before writing; supersede over duplicate.

- **Modules:** `src/knowledge_graph.py`, `src/storage/knowledge_graph_age.py`, `src/mcp_handlers/knowledge/`. Semantics: [`dev/KNOWLEDGE_GRAPH_SEMANTICS.md`](dev/KNOWLEDGE_GRAPH_SEMANTICS.md).
- **Maturity: LIVE / strong bones, two named gaps.** Provenance tagging and cross-agent linking are solid and differentiated. (a) The objective retrieval eval now drives the **live serving path** (the same handler `knowledge(action="search")` uses; [issue #1050](https://github.com/cirwel/unitares/issues/1050) closed 2026-06-26), but the committed baselines predate that fix, so no current serving-path score is on record, and the labeled corpus remains small and skewed toward distinctive-term queries. (b) With the AGE knowledge backend, the AGE graph is **live and 1:1-synced** but **advisory** — graph *traversal/reasoning* is dormant; canonical lineage uses a recursive CTE (AGE 1.7 can't express the causal path filter). It is *not* "dormant/off"; it *is* "not canonical."

## Review: dialectic and recovery

When an agent's state degrades, or a verdict or claim is contested, it can
escalate to a **dialectic review**: a separate reasoner argues the case
(thesis / antithesis / synthesis, typed messages, resolution tracking) instead
of a rubber stamp. The record keeps positions, objections, conditions, and
resolution; requesting review does not complete it. The orchestrated
independent reviewer is **de-inert and serving live reviews** in the reference
deployment, where `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW` is on (dispute →
fail-closed).

**Who reviews.** A dialectic review (`request_review`; thesis -> antithesis ->
synthesis) is answered by one of:

- **Orchestrated reviewer** — a separate process with its own identity
  (`agents/dialectic_reviewer/`; local, Codex, Claude or external backends).
  When `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW` is on (default off in code) it
  is the first choice; see
  [`proposals/active/orchestrated-dialectic-reviewer-v0.md`](proposals/active/orchestrated-dialectic-reviewer-v0.md)
- **In-process synthetic reviewer** — the default, and the fallback when
  orchestration is on. Its verdict is binding (it approves only a RESUME
  synthesis whose antithesis does not dispute), but it runs in the caller's
  process under a synthetic reviewer id, so it is not an independent reviewer
- **Peer agent** — another agent takes the antithesis role

**Recovering from a pause.** A pause refuses check-ins and new shared-memory
entries (not queued); dialectic moves still work. A paused agent's risk is
frozen at the reading that paused it, because it cannot write the check-in that
would lower it, so most pauses end through a dialectic review, an operator
(`operator_resume_agent`), or re-evaluation at auto-expiry
(`src/mcp_handlers/support/pause_ttl.py`). The paths:

1. **Self-recovery** — `self_recovery(action="check")` reports eligibility.
   `quick` if risk ≤ 0.40 and no void is active; `review` with a reflection
   below risk 0.65. It lifts a pause only when the reading that paused the
   agent is already under those gates. Legacy `C(V)` is diagnostic context only
2. **Dialectic review** — any of the reviewers above, through `request_review`.

See [dev/CIRCUIT_BREAKER_DIALECTIC.md](dev/CIRCUIT_BREAKER_DIALECTIC.md) for the full protocol.

- **Modules:** `src/dialectic_db.py`, `src/mcp_handlers/dialectic/`, the orchestrated-review path (launchd-hosted reviewer).
- **Maturity: IN-FLIGHT / perpetually refined.** The core flow is live, but reviewer *quality* and dispatch strategy are open work — this is the component most continuously under revision. Root cause of earlier weakness (a non-independent, rubber-stamping reviewer) is closed; making the independent reviewer *good* is the live frontier.

## Outcomes

`record_result` (canonical `outcome_event`) records confidence and outcomes such
as test results, command exit codes and review labels, with an evidence grade
and, when supplied, the prediction it grades. Preserve who produced each
outcome when assessing its independence: an outcome record is a claim with
provenance, not independent proof. CI-, tool- or operator-authored evidence is
a stronger anchor than an outcome authored by the monitored process, and a
process that controls both its confidence and every outcome record can forge a
consistent story ([threat model](SCOPE_AND_THREAT_MODEL.md)).

Outcomes also feed calibration and the EISV instrument; how they enter E and I
is in [EISV_COMPUTATION.md](EISV_COMPUTATION.md#step-1--observations-srcbehavioral_sensorpy).

- **Modules:** `src/mcp_handlers/observability/outcome_events.py`, `src/grounding/outcome_anchors.py`, `src/auto_ground_truth.py`, `src/calibration.py`, `audit.outcome_events`.

Reconstruction is a client workflow over these layers — knowledge search,
dialectic reads, history export (`src/mcp_handlers/introspection/export.py`),
and operator-gated outcome-evidence reads — with the authorization and
retention boundaries listed in [capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md#start-with-the-core-workflow).

## The check-in and policy action

An agent calls `process_agent_update` (advertised to agents as `sync_state`) with:
- `response_text` — what it did; primary operational input
- `complexity` — optional reflective self-report [0, 1]
- `confidence` — optional reflective self-report [0, 1]

At runtime, the reflective fields above (`complexity`, `confidence`) are not trusted in isolation. The dual-log layer (`src/dual_log/`) compares them against server-derived operational signals, tool usage, continuity metrics, and other exogenous evidence when available. Each check-in keeps a bounded excerpt of its report text (`src/report_excerpt.py`), not the full text; durable claims belong in a finding.

The check-in returns an EISV behavioral state estimate and a policy action. The decision engine (`src/monitor_decision.py`) returns one of two actions, `proceed` or `pause`; `guide` is a `sub_action` of `proceed` that the response surfaces as its own policy action:

| Policy action | Meaning | Agent action |
|---------|---------|-------------|
| `proceed` | Policy permits continuation | Continue working |
| `guide` | `proceed` with guidance — slightly off track or at a basin boundary | Read guidance, adjust approach |
| `pause` | A hard stop: check-ins and new shared-memory entries are refused, not queued | Stop, read the reason, `self_recovery(action="check")` for self-recovery eligibility |

`reject` is no longer an action: it appears as a `sub_action` of `pause` on the risk-threshold path, and is accepted as a legacy input alias that normalizes to `pause`. Responses also carry a `margin` reporting proximity to the nearest decision threshold. A pause holds governed writes; enforcement outside those writes requires host integration.

**Who owns what.** [Manual §5.3](manual/05-reading-the-signals.md#53-the-policy-action) owns agent-facing handling of each action and sub-action. [EISV_COMPUTATION.md](EISV_COMPUTATION.md) owns how the state and risk are computed: observation blends, EMA smoothing, warmup stages, the drift vector, risk components, margin levels, the ODE coupling, and the oscillation posture near a threshold, together with its evidence caveats (the frozen ablation read is a `NON_DETECTION`, no prevention is demonstrated, and the deployed heuristic blend is not the Paper v6 target formulation).

- **Maturity: DEPLOYED.** The indices, assessment and policy paths are inspectable. Evidence for particular diagnostic or decision benefits is tracked separately in the [claim ledger](EVIDENCE_AND_LIMITS.md#current-claim-status).

**Assessment failure is fail-open.** If the assessment pipeline itself errors (DB/Redis outage, internal bug — distinct from the BEAM proxy, which fails open to in-process Python), the check-in handler returns an error response with **no `verdict`/`action` field** — it does not synthesize a `pause`. The agent proceeds. This is intentional for an advisory, non-guardrail system: fail-*closed* would let a transient infra blip mass-pause the whole fleet, and the anyio/asyncpg/Redis latency class (see *asyncpg, Redis and the anyio scheduler* under Database Architecture) makes that a live risk. The normal pause gates (CIRS / void / coherence / high-risk / low-basin) are unaffected; this covers only the case where no verdict could be computed at all. See the design comment at `src/mcp_handlers/core.py` (`process_agent_update` handler).

## Coordination: the lease plane

A BEAM (Elixir/OTP) kernel for single-writer coordination and liveness on
shared surfaces (Plexus), governed effects, and agent messaging. Port 8788,
bearer-gated, fail-closed.

- **Modules:** `lease_plane.*`, the `dispatch_beam` client; runbook in [`operations/lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md).
- **Maturity: PARTIAL** — advisory-first rollout; Wave 3a read-only cutovers live; Wave 3 proper is committed and open with no active implementation (see [`proposals/active/beam-wave-3-handler-dispatch.md`](proposals/active/beam-wave-3-handler-dispatch.md)).

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
|  metrics.series              |     metrics time series
|  lease_plane.*, effects.*,   |     BEAM coordination planes
|  coordination.*,             |
|  orchestration.*             |
|                              |
|  Redis (port 6379)           |     De-facto primary session store —
|  audit_log.jsonl (raw)       |     not optional; degraded local-only without it.
+------------------------------+
```

**Ownership is simple:** PostgreSQL+AGE is the single source of truth for all governance data. The substrate is **mature**: one Postgres (relational + Apache AGE 1.7 + pgvector), with Redis as the de-facto primary session store (not optional), being migrated to a PG mirror.

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
| `src/mcp_server.py` | MCP server entry point |
| `src/mcp_handlers/identity/` | Identity binding, lineage, proof tiers |
| `src/knowledge_graph.py` | Knowledge-store factory (FTS / AGE backends) |
| `src/mcp_handlers/dialectic/handlers.py` | Thesis/antithesis/synthesis |
| `src/mcp_handlers/observability/outcome_events.py` | Outcome events (`record_result`) |
| `src/mcp_handlers/core.py` | `process_agent_update` handler |
| `src/monitor_decision.py` | Final policy action |
| `config/governance_config.py` | Thresholds, margin computation |
| `governance_core.dynamics` | EISV differential equations |
| `governance_core.coherence` | Coherence function C(V, Theta) |
| `governance_core.adaptive_governor` | PID controller, oscillation detection |
| `src/mcp_handlers/lifecycle/stuck.py` | Stuck detection, auto-recovery (`lifecycle/handlers.py` re-exports) |
| `src/mcp_handlers/lifecycle/self_recovery.py` | `self_recovery` dispatcher and quick path |
| `src/mcp_handlers/lifecycle/operations.py` | `self_recovery` review path (`handle_self_recovery_review`) |
| `src/calibration.py` | Confidence -> correctness mapping |
| `src/mcp_handlers/cirs/` | CIRS v2 protocol (7 message types) |

---

## Threat model and security posture

The deployment defaults are listed under [Security defaults](#interfaces-and-transport-surfaces) above. Multi-tenant or public-facing deployment needs a harder auth posture than those defaults. The adversarial boundaries and the open gap in sustained adversarial testing are in [`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md#scope-and-threat-model); container hardening and its remaining gaps are in [`DEPLOYMENT_SECURITY.md`](DEPLOYMENT_SECURITY.md); the single-operator deployment record is in [`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md). Vulnerability reports: [`SECURITY.md`](../.github/SECURITY.md). To verify claims yourself, use the [Reviewer Guide](REVIEWER_GUIDE.md); the evaluation surface is indexed in [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md).

## What's in flight

Current priorities are in [`ROADMAP.md`](ROADMAP.md). Shipping RFCs and their status (Built / Active / Parked) are indexed in [`proposals/active/README.md`](proposals/active/README.md); the identity ontology and its RFCs start at [`ontology/README.md`](ontology/README.md). The paper is [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)); how v7 was positioned is recorded in [`ontology/paper-positioning.md`](ontology/paper-positioning.md).

If runtime code and this doc disagree, runtime wins. Disputes resolve against [`dev/CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md).
