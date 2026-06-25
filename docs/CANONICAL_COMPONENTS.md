# UNITARES — Canonical Components

The system as a set of **modules/layers**, bottom-up. This is the orthogonal view to
[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md), which traces a single check-in through the
*pipeline*; this doc is the *component* map — what UNITARES is made of, and how mature each part is.

Each component states what it is, its key modules, and its **honest current maturity** (not a
roadmap aspiration). Where a component has a known limit, it's named here, not buried.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ SURFACES        MCP /mcp/ · REST /v1 · Dashboard · SDK · governance plugin      │  reach
├──────────────────────────────────────────────────────────────────────────────┤
│ EISV & GOVERNANCE   state vector → risk → verdict (proceed/guide/pause/reject)  │  "is this
│ DIALECTIC ENGINE    independent review when state degrades or a verdict is      │   agent
│                     contested — reasoning, not a rubber stamp                    │   healthy?"
├──────────────────────────────────────────────────────────────────────────────┤
│ KNOWLEDGE GRAPH     shared, provenance-tagged cross-agent memory                │  what the
│                                                                                  │  fleet knows
├──────────────────────────────────────────────────────────────────────────────┤
│ IDENTITY & ONTOLOGY  per-instance UUID · lineage DAG · proof tiers              │  who did what
│                      — anchors every write and verdict above it                  │  (the base)
├──────────────────────────────────────────────────────────────────────────────┤
│ SUBSTRATE       one Postgres (relational + Apache AGE + pgvector) ·             │  durable truth
│                 BEAM lease plane (coordination) · resident agents                │  + coordination
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Identity & Ontology — the foundation

Every action in the system attributes to a **per-instance identity**: a registry UUID minted on a
fresh process (`onboard(force_new=true)`), distinct from its public handle and its cosmetic label
(the s22 identity ontology — *identity is the UUID; the name is social*). Identity carries a
**proof tier** (`weak` → `strong`, strengthened by echoing a `continuity_token`), a **lineage DAG**
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
- **Maturity: LIVE / strong bones, two named gaps.** Provenance tagging and cross-agent linking are solid and differentiated. (a) Retrieval quality on the **default serving backend** is currently **unmeasured** — the objective eval is broken on it ([issue #1050](https://github.com/cirwel/unitares/issues/1050)); it scores well (nDCG ≈ 0.94) only via the AGE backend on a small distinctive-term corpus. (b) The AGE graph is **live and 1:1-synced** but **advisory** — graph *traversal/reasoning* is dormant; canonical lineage uses a recursive CTE (AGE 1.7 can't express the causal path filter). It is *not* "dormant/off"; it *is* "not canonical."

## 3. Dialectic Engine — independent reasoning

When an agent's state degrades, or a verdict is contested, governance can escalate to a **dialectic
review**: a separate reasoner argues the case (thesis / antithesis / synthesis, typed messages,
resolution tracking) instead of a rubber stamp. The orchestrated independent reviewer is now
**de-inert and serving live reviews** (dispute → fail-closed).

- **Modules:** `src/dialectic_db.py`, `src/mcp_handlers/dialectic/`, the orchestrated-review path (launchd-hosted reviewer).
- **Maturity: IN-FLIGHT / perpetually refined.** The core flow is live, but reviewer *quality* and dispatch strategy are open work — this is the component most continuously under revision. Root cause of earlier weakness (a non-independent, rubber-stamping reviewer) is closed; making the independent reviewer *good* is the live frontier.

## 4. EISV & Governance — state model + verdicts

The "is this agent still healthy?" loop. **EISV** is a 4-dimensional behavioral state vector —
**E**nergy (is work advancing), **I**ntegrity (do claims match results), **E**ntropy/**S** (drifting
from its own normal), **V**alence (energy-vs-integrity) — graded against the agent's *own* baseline.
It coheres into a risk score, a **verdict** (`proceed`/`guide`/`pause`/`reject`), and a **basin**
(healthy / boundary / degraded), with calibration against objective outcomes (test exit codes, tool
results). The system runs **two loops**: the deployed behavioral verdict path (EMA + z-score + health
gate) drives decisions; a parallel ODE / free-energy formulation runs as a research lens and **does
not** drive verdicts.

- **Modules:** `src/behavioral_state.py` + `src/behavioral_assessment.py` (verdict path); `governance_core/dynamics.py` (ODE); `src/grounding/free_energy.py` (tiered; FEP explicitly stubbed).
- **Honest competence boundary:** EISV's Integrity is a **confidence-vs-outcome consistency detector** — it catches *naive overconfidence and drift* in cooperative agents, and is **not** a deliberate-concealment detector (it collapses on noisy baselines and inverts on *calibrated* concealment; cross-framing is the right tool for that). The information-theoretic / free-energy framing is the research **target**, not the live path. See [`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md), [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md), and the model-organism / real-LLM evals in [`demos/frt_autonomy_sandbagging/`](../demos/frt_autonomy_sandbagging/).
- **Maturity: DEPLOYED as calibration-observability with characterized limits.** Conceptually the heaviest layer — today it's *the part you have to learn* more than the clean headline; it could become the headline once the external validity gap closes.

## Supporting components

| Component | What it is | Key modules | Maturity |
|---|---|---|---|
| **Coordination / lease plane** | BEAM (Elixir/OTP) kernel for single-writer coordination + liveness on shared surfaces (Plexus). Port 8788, bearer-gated. | `lease_plane.*`, the `dispatch_beam` client | **PARTIAL** — advisory-first rollout; Wave 3a first cutovers live |
| **Resident agents** | Always-on governed agents | Vigil (cron janitorial) · Sentinel (continuous analytical) · Watcher (PostToolUse) · Steward (Pi→Mac) · Chronicler (daily) · Lumen (embodied Pi) | **LIVE** (launchd) |
| **Substrate** | Durable truth | ONE Postgres = relational + Apache AGE 1.7 + pgvector; Redis optional | **MATURE** |
| **Surfaces** | How agents/humans reach it | MCP `/mcp/` · REST `/v1/tools/call` · Dashboard `/dashboard` · SDK · governance plugin (Claude Code/Codex hooks) · host-adapter | **LIVE** |

---

## Maturity at a glance

| Component | Status | One-line honest read |
|---|---|---|
| Identity & Ontology | **Mature** | Load-bearing; strict write gate live; anchors everything |
| Knowledge Graph | **Live** | Strong provenance + cross-agent linking; serving-path retrieval unmeasured; AGE live-but-advisory |
| Dialectic Engine | **In-flight** | Independent reviewer serving live; reviewer-quality is the open frontier |
| EISV & Governance | **Deployed (bounded)** | Calibration-observability; catches naive overconfidence/drift, not deliberate concealment |
| Coordination (BEAM) | **Partial** | Advisory-first; first cutovers live |
| Residents · Substrate · Surfaces | **Live / Mature** | Operational |

**See also:** [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) (pipeline/flow view) · [`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md) (who it's for, what's unproven) · [`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md) (verify it yourself) · [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md) (the eval surface).
