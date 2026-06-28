# Knowledge Graph — flow, consistency, and usage

How the shared discovery store actually behaves. Source of truth is the code
(`src/mcp_handlers/knowledge/handlers.py`, `src/knowledge_graph.py`,
`db/postgres/knowledge_schema.sql`); this doc captures the semantics that are
otherwise only discoverable by reading it.

## Agent-to-agent flow

The KG is shared memory: one agent records a discovery, another finds and
builds on it. All actions route through the consolidated `knowledge(...)` tool
(aliases: `search_shared_memory` → search, `record_result` → outcome).

1. **A writes** — `knowledge(action="store", summary=..., discovery_type=...,
   severity=..., tags=[...])` → a discovery with `id` (UTC-timestamp), the
   author's `agent_id`, `status="open"`. Emits a `knowledge_write` audit event.
2. **B searches** — `knowledge(action="search", query=...)` → results carry
   **writer attribution** (`by` = author label at write time or live display
   name, plus `agent_id`), so B knows whose discovery it is. Emits a
   `knowledge_read` event.
3. **B responds** — `knowledge(action="answer_question",
   response_to={discovery_id, response_type}, summary=...)` → a new discovery
   linked back via `response_to_id`, forming a chain queryable through
   `get_response_chain()`.

> Write discipline (see CLAUDE.md "Strict Identity"): **search before writing.**
> If a related entry exists, prefer a linked correction or `supersede` over a
> fresh note.

## Write / consistency semantics

The store is **last-write-wins with no optimistic locking** — there is no
version column and no conflict detection. Specifically:

- **`INSERT ... ON CONFLICT (id) DO UPDATE`** (`src/db/mixins/knowledge_graph.py`):
  two writes with the same `id` collapse — the second silently UPDATEs the
  first (summary, details, tags, status, provenance_chain, updated_at).
- **ID minting** (`_new_discovery_id`): strictly monotonic within a single
  process (bumps 1µs on collision). **Residual risk:** two *separate* processes
  can still mint the same microsecond id; closing that fully is a contract
  change, not yet made.
- **Edit ownership** is enforced only on **high/critical** discoveries: a
  non-owner may move status to `{resolved, closed, wont_fix}` but cannot edit
  content/metadata. Low-severity discoveries are freely multi-agent editable.

### Status lifecycle

Valid statuses (`VALID_DISCOVERY_STATUSES`, mirrored by a CHECK constraint):
`open`, `resolved`, `archived`, `disputed`, `closed`, `wont_fix`, `superseded`.

There is **no state machine** — any status may transition to any other; updates
validate membership only, not the transition. Treat the lifecycle as advisory.

### Links between discoveries

- `response_to_id` + `response_type` — relational parent link for dialectic
  chains. Response types: `extend`, `question`, `disagree`, `support`,
  `answer`, `follow_up`, `correction`, `elaboration`, `supersedes`.
- `discovery_edges` — graph edges (AGE backend only).
- **supersession** — marks the old row `status="superseded"` and records a
  `supersedes` edge to the successor. The edge recording is best-effort and
  depends on the AGE backend being active.

## Usage / read auditing

Writes have always been audited; **reads are audited too**, via
`knowledge_read` events (`_broadcast_knowledge_read`) into `audit.events`. Each
read records the **reader** `agent_id` and, when knowable (`details`/`get`
exact, `search` sampled), the **writer** `agent_id` — enough to distinguish
self-reads from cross-agent reads in SQL.

To answer *"is the shared memory actually consulted, and cross-agent?"* without
hand-writing SQL, use the read-only report:

```bash
python3 scripts/analysis/kg_usage_report.py --window-days 90
# --json for machine output; GOVERNANCE_DATABASE_URL selects the DB
```

It reports write/read volume, reads-by-action, self vs cross-agent reads, top
reader→writer pairs, and a self-grading verdict. It deliberately flags
**reader concentration**: a high cross-agent count dominated by one reader is
usually a resident sweeper bulk-searching the corpus, not broad peer-to-peer
use — the verdict says so rather than letting the headline mislead.

### Metrics

`KNOWLEDGE_NODES_TOTAL` (`unitares_knowledge_nodes_total`) is the live corpus
size — total `knowledge.discoveries`. It is set by the KG lifecycle background
task (`run_kg_lifecycle_cleanup`) at startup and on its periodic sweep, as an
absolute `COUNT(*)` rather than incremented on store — so it survives restarts
and reflects deletes/archival. It is deliberately **not** set in the `/metrics`
handler, which must stay DB-free (no `await` on asyncpg in the scrape path).
The refresh cadence follows the lifecycle task (startup + periodic); for
on-demand size/usage breakdowns use `scripts/analysis/kg_usage_report.py`.
