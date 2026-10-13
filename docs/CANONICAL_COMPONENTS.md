# UNITARES — Canonical Components

**Last reviewed:** 2026-08-26

The system as a set of **modules/layers**, bottom-up. This is the orthogonal view to
[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md), which traces a single check-in through the
*pipeline*; this doc is the *component* map — what UNITARES is made of, and how mature each part is.

Each component states what it is, its key modules, and its **honest current maturity** (not a
roadmap aspiration). Where a component has a known limit, it's named here, not buried.

UNITARES Core does not own the agent execution loop. Agent harnesses, custom
clients, and reference residents sit above the public interfaces and remain
independent userlands. MCP is the primary agent-facing contract, while REST,
the SDK, adapters, and the dashboard expose the same Core. The general-purpose
**UNITARES Resident** belongs in that userland layer and must use those public
contracts; its separate repository ships an early skeleton, and it is not a
deployed Core component.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ USERLANDS       agent harnesses · custom clients · reference residents          │  run agents
├──────────────────────────────────────────────────────────────────────────────┤
│ INTERFACES      MCP /mcp/ · REST /v1 · Dashboard · SDK · governance plugin      │  reach Core
├──────────────────────────────────────────────────────────────────────────────┤
│ EISV & GOVERNANCE   state vector → risk → verdict (proceed/guide/pause/reject)  │  state +
│ DIALECTIC ENGINE    independent review when state degrades or a verdict is      │  policy +
│                     contested — reasoning, not a rubber stamp                    │  review
├──────────────────────────────────────────────────────────────────────────────┤
│ KNOWLEDGE GRAPH     shared, provenance-tagged cross-agent memory                │  what the
│                                                                                  │  fleet knows
├──────────────────────────────────────────────────────────────────────────────┤
│ IDENTITY & ONTOLOGY  per-instance UUID · lineage DAG · proof tiers              │  who did what
│                      — anchors every write and verdict above it                  │  (the base)
├──────────────────────────────────────────────────────────────────────────────┤
│ SUBSTRATE       one Postgres (relational + Apache AGE + pgvector) · Redis ·     │  durable truth
│                 BEAM lease plane (coordination)                                  │  + coordination
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Identity & Ontology — the foundation

Every action in the system attributes to a **per-instance identity**: a registry UUID minted on a
fresh process (`onboard(force_new=true)`), distinct from its public handle and its cosmetic label
(the s22 identity ontology — *identity is the UUID; the name is social*). Identity carries a
**proof tier** (`weak` → `strong`; ordinary continuation echoes the returned
`client_session_id`, while a short-lived `continuity_token` is reserved for an explicit
same-live-process rebind), a **lineage DAG**
(`parent_agent_id` / `spawn_reason`, declared not inferred), and a **strict write gate** — reads may
be anonymous, writes must be accountable.

- **Modules:** `src/mcp_handlers/identity/`, `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/schemas/identity.py`, `core.identities`.
- **Invariants (structural, do not re-introduce):** never auto-`force_new`; no lookup-by-label; first MCP call is the sole identity source; co-location is not lineage.
- **Maturity: MATURE / load-bearing.** Strict identity is enforced in production. This layer **anchors the KG** (every discovery has an `Agent` vertex + `AUTHORED` edge) and every verdict (state attributes to a UUID).

## 2. Knowledge Graph — shared, provenanced memory

What one agent learns, the fleet keeps. Discoveries are written to a shared store where **every value
is provenance-tagged** (`measured` / `derived` / `prior`) so a stored memory never reads as more
certain than it is. One Postgres database holds **three representations** of each discovery:

- **relational** `knowledge.discoveries` — the canonical record (+ FTS).
- **pgvector** `core.discovery_embeddings_bge_m3` — semantic search.
- **AGE property graph** `governance_graph` — `Agent`/`Discovery`/`Tag` vertices; `AUTHORED`/`RELATED_TO`/`SUPERSEDES`/`SPAWNED`/`TAGGED` edges — the live relationship layer (supersession chains, lineage, relatedness).

Retrieval is hybrid (vector + FTS, RRF-fused) and **honestly self-reports low confidence** when a
query matches only semantically. Write discipline: search before writing; supersede over duplicate.

- **Modules:** `src/knowledge_graph.py`, `src/storage/knowledge_graph_age.py`, `src/mcp_handlers/knowledge/`.
- **Maturity: LIVE / strong bones, two named gaps.** Provenance tagging and cross-agent linking are solid and differentiated. (a) The objective retrieval eval now drives the **live serving path** (the same handler `knowledge(action="search")` uses; [issue #1050](https://github.com/cirwel/unitares/issues/1050) closed 2026-06-26), but the committed baselines predate that fix, so no current serving-path score is on record, and the labeled corpus remains small and skewed toward distinctive-term queries. (b) The AGE graph is **live and 1:1-synced** but **advisory** — graph *traversal/reasoning* is dormant; canonical lineage uses a recursive CTE (AGE 1.7 can't express the causal path filter). It is *not* "dormant/off"; it *is* "not canonical."

## 3. Dialectic Engine — independent reasoning

When an agent's state degrades, or a verdict is contested, governance can escalate to a **dialectic
review**: a separate reasoner argues the case (thesis / antithesis / synthesis, typed messages,
resolution tracking) instead of a rubber stamp. The orchestrated independent reviewer is now
**de-inert and serving live reviews** (dispute → fail-closed).

- **Modules:** `src/dialectic_db.py`, `src/mcp_handlers/dialectic/`, the orchestrated-review path (launchd-hosted reviewer).
- **Maturity: IN-FLIGHT / perpetually refined.** The core flow is live, but reviewer *quality* and dispatch strategy are open work — this is the component most continuously under revision. Root cause of earlier weakness (a non-independent, rubber-stamping reviewer) is closed; making the independent reviewer *good* is the live frontier.

## 4. EISV & Governance — state model + verdicts

**EISV** records runtime observations and controller history as four smoothed
indices. With behavioral-sensor inputs, E blends decision history, controller
coherence level and complexity divergence; I blends calibration error and
controller coherence trend; S blends
drift, regime transitions and complexity divergence. Available outcome,
continuity and tool features also contribute. V separately smooths the raw
`E_obs − I_obs` gap; its distinct smoothing rate preserves temporal information
beyond current E minus current I. Physical sensor inputs can enter the same
smoothing stage; inspect input provenance as well as the output path named by
`primary_eisv_source`.

Behavioral risk assessment compares these indices with fixed thresholds and,
after warmup, the agent's own baseline. Final policy also uses ODE state for
structural basin, void and coherence checks. ODE history feeds later behavioral
observations, and prior decisions feed E: the instrument includes the
controller's response. The [computation reference](EISV_COMPUTATION.md) defines
each producer and transformation.

- **Modules:** `src/behavioral_sensor.py` + `src/behavioral_state.py` + `src/behavioral_assessment.py` (behavioral path); `src/monitor_decision.py` (policy); `governance_core/dynamics.py` (ODE); `src/grounding/free_energy.py` (tiered; FEP explicitly stubbed).
- **Interpretation:** I's deployed calibration input is a lifetime fleet strategic aggregate, not a check of this agent's current claim. Its optional outcome-consistency term measures variance, so consistent failure and consistent success both score as consistent; outcome success enters E separately. Matching reported confidence to suppressed performance can still resemble honest lower capability in the monitored trace. See the [threat model](SCOPE_AND_THREAT_MODEL.md) and [concealment boundary fixture](../tests/test_calibrated_capability_concealment.py).
- **Maturity: DEPLOYED.** The indices, assessment and policy paths are inspectable. Evidence for particular diagnostic or decision benefits is tracked separately in the [claim ledger](EVIDENCE_AND_LIMITS.md#current-claim-status).

## Supporting components

| Component | What it is | Key modules | Maturity |
|---|---|---|---|
| **Coordination / lease plane** | BEAM (Elixir/OTP) kernel for single-writer coordination + liveness on shared surfaces (Plexus). Port 8788, bearer-gated. | `lease_plane.*`, the `dispatch_beam` client | **PARTIAL** — advisory-first rollout; Wave 3a read-only cutovers live; Wave 3 proper is committed and open with no active implementation (see [`proposals/active/beam-wave-3-handler-dispatch.md`](proposals/active/beam-wave-3-handler-dispatch.md)) |
| **Reference resident agents** | Specialized, optional governed clients; examples rather than Core internals or a general agent framework. | `agents/` and independently deployed residents | **LIVE** in the maintainer deployment |
| **Substrate** | Durable truth | ONE Postgres = relational + Apache AGE 1.7 + pgvector; Redis = de-facto primary session store (not optional), being migrated to PG-mirror | **MATURE** |
| **Surfaces** | How agents/humans reach it | MCP `/mcp/` · REST `/v1/tools/call` · Dashboard `/dashboard` · SDK · governance plugin (Claude Code/Codex hooks) · host-adapter | **LIVE** |

---

## Maturity at a glance

| Component | Status | One-line honest read |
|---|---|---|
| Identity & Ontology | **Mature** | Load-bearing; strict write gate live; anchors everything |
| Knowledge Graph | **Live** | Strong provenance + cross-agent linking; serving-path eval fixed, current score not yet on record; AGE live-but-advisory |
| Dialectic Engine | **In-flight** | Independent reviewer serving live; reviewer-quality is the open frontier |
| EISV & Governance | **Deployed** | Runtime observations and controller history → smoothed indices → assessment and policy |
| Coordination (BEAM) | **Partial** | Advisory-first; Wave 3a read-only cutovers live; Wave 3 committed, not started |
| Reference residents · Substrate · Interfaces | **Live / Mature** | Operational; UNITARES Resident has a separate early skeleton, not yet a usable agent product |

**See also:** [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) (pipeline/flow view) · [`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md) (who it's for, what's unproven) · [`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md) (verify it yourself) · [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md) (the eval surface).
