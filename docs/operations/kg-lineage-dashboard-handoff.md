# Handoff: KG supersession / correction lineage on the dashboard

**Goal:** let an operator click a knowledge-graph discovery and see its
**lineage** — what it supersedes, and what later superseded it — plus its
related (similarity) neighbours. This is the "history" the dashboard can't show
for the KG yet (dialectic transcripts and agent EISV history already ship).

This is an **exposure** task, not a graph-modeling one. The links already exist;
the read API just doesn't return them.

> **Correction note (2026-06-21).** An earlier draft of this handoff specced the
> wrong columns. It claimed lineage lived in `response_to_id` / `response_type`
> and that "AGE has no supersession label — use the relational columns." Both are
> wrong, verified live (see below). `response_to_id` / `response_type` are
> **NULL on 100% of rows** (that typed-response feature has never been used), and
> **supersession is stored as a `SUPERSEDES` edge in AGE**, the opposite of what
> the draft said. This version points at the real data. Read the **Scope reality**
> section before deciding to build — the authored signal is small.

## Scope reality (verified 2026-06-21, live DB — read this first)

> **Prerequisite (2026-06-21 update): the lineage data did not exist when this
> handoff was first written, and barely exists now.** A live check found 18 rows
> with `status='superseded'` but **0 SUPERSEDES edges** and 0 `response_to_id` —
> the directed "what replaced what" link was recorded *nowhere* (the supersede
> write path dropped `superseded_by`). **PR #991 fixes the write path** so
> superseding now creates the AGE `SUPERSEDES` edge. Until #991 is merged +
> deployed and new supersessions accumulate, this dashboard has **nothing to
> render** beyond a "superseded" badge + the related (similarity) list. **This
> dashboard is deferred until edges accumulate.**

| Signal | Where it lives | Rows (of 1091) |
|---|---|---|
| **Supersession** (`A SUPERSEDES B`) | **AGE graph edge** `(:Discovery)-[:SUPERSEDES]->(:Discovery)` + the old row's `status='superseded'` | **0 edges today** (18 status-only rows; edges accumulate after #991) |
| `response_to_id` (typed-response parent) | relational column | **0** — never written |
| `response_type` (edge label enum) | relational column | **0** — never written |
| **related** (similarity neighbours) | `related_to text[]` (auto-computed at store time) | **643** (~59%) |

Implications for whoever picks this up:
- The headline "what does this supersede / correct" view is driven by
  **SUPERSEDES edges — of which there are 0 today** (18 notes are *marked*
  superseded but carry no link). It only becomes worth building once #991 is
  live and real supersessions have accumulated edges. Size the effort to the
  (currently near-zero, growing) payoff.
- **`related_to` is NOT lineage.** It is auto-computed semantic-similarity
  neighbours written by the store path, not an authored "this corrects that"
  edge. Surface it as a **separate, clearly-labelled "related" list** — do not
  fold it into "supersedes/history" or you mislabel similarity as authorship.
- Ignore `response_to_id` / `response_type` entirely for now (empty). If the
  typed-response feature ever gets used, revisit.

## Why it isn't done yet

The redesign discovery cards (`dashboard/redesign/sections/discoveries.js`)
surface summary, provenance, details, and tags, but the *links between*
discoveries are invisible because `knowledge(action="search")` strips relation
fields from its result.

## The data model (verified)

`knowledge.discoveries` columns that exist: `id` (text), `status` (text — value
`superseded` on superseded rows), `related_to` (text[]), `provenance_chain`
(present but **empty on the superseded rows checked** — do not rely on it),
`response_to_id` / `response_type` (both 100% NULL — unused).

Supersession is a **graph edge**, created by `KnowledgeGraphAGE.supersede_discovery`
(`src/storage/knowledge_graph_age.py:2449`, via `create_supersedes_edge(new_id, old_id)`):

```
(newer:Discovery)-[:SUPERSEDES]->(older:Discovery)
```

The edge points **from the newer discovery to the one it replaces**. The
connectivity scorer already reads it (`knowledge_graph_age.py:2029`,
`OPTIONAL MATCH (newer)-[s:SUPERSEDES]->(d) ... count(...) as superseded_by`) —
copy that read shape.

So a discovery `d`'s lineage is:
- **ancestors** (what `d` supersedes): `MATCH (d)-[:SUPERSEDES]->(older)`;
- **descendants** (what superseded `d`): `MATCH (newer)-[:SUPERSEDES]->(d)`;
- **related** (similarity, separate): the `related_to` array (relational).

### Depth / traversal caveat (don't skip)

AGE 1.7 **cannot reliably filter a variable-length path** (the dual-store verdict
— `knowledge.discoveries` is relational-authoritative, AGE advisory; see the KG
dual-store architecture note). So do **not** write `[:SUPERSEDES*1..N]` and trust
it. Two honest options:

1. **v1 (recommended, matches current scale): bounded 1–2 hop read.** Supersession
   chains are realistically ≤2–3 deep and edge counts are small (0 before #991,
   accumulating after). A single
   1-hop query each direction (optionally iterated a fixed 2–3 times in Python)
   covers it without variable-length Cypher. Simple, correct, cheap.
2. **If chains grow or you want relational-authoritative cleanliness:** backfill a
   relational `superseded_by text` column from the existing AGE edges (one-time)
   and have `supersede_discovery` maintain it going forward, then walk it with a
   depth-bounded recursive CTE. Bigger lift; defer until the data warrants it.

## Backend (recommended)

Mirror the agent-history endpoint already in the repo — `http_agent_history`
(`src/http_api.py:1025`, route `/v1/agents/{agent_id}/history`, PR #935) is the
exact template: bearer-auth via `_check_http_auth` / `_http_unauthorized`,
`db.acquire()`, `JSONResponse`, graceful-empty, registered with
`app.routes.append(Route(..., methods=["GET"]))` in `register_http_routes`
(`http_api.py:3101`).

```
GET /v1/discoveries/{id}/lineage
```

returns:

```json
{
  "id": "<id>",
  "status": "superseded | open | ...",
  "supersedes":    [{"id","summary","type","created_at","by"}],
  "superseded_by": [{"id","summary","type","created_at","by"}],
  "related":       [{"id","summary","type"}]
}
```

- `supersedes` = ancestors `(d)-[:SUPERSEDES]->older`; `superseded_by` =
  descendants `newer-[:SUPERSEDES]->(d)`. Run the bounded SUPERSEDES read against
  AGE (copy the `knowledge_graph_age.py:2029` cypher shape via the same
  graph_query path it uses; the wrapper was hardened in #794). Hydrate ids →
  summaries from `knowledge.discoveries` in one relational `WHERE id = ANY(...)`
  (avoid N+1).
- `related`: `SELECT ... FROM knowledge.discoveries WHERE id = ANY((SELECT related_to FROM knowledge.discoveries WHERE id = $1))`.
- Empty arrays when there's nothing — never error on a discovery with no lineage.

(Alternative: expose `related_to` + a `has_supersession` flag in the existing
`knowledge` search result and resolve client-side. Simpler to wire, but no graph
read and N+1 for summaries — the dedicated endpoint is cleaner.)

## Frontend

Reuse the **dialectic-transcript lazy-load** pattern (PR #981) — the model to copy:
- `dashboard/redesign/sections/dialectic.js` → the `renderTranscript` helper + the
  `<details class="dlc-transcript">` toggle that lazy-fetches on first expand.
- `dashboard/redesign/data.js` → `dialecticSession(id)` accessor.

Do the same here:
- `data.js`: add `discoveryLineage(id)` → calls the new endpoint, `withFallback`.
- `discoveries.js`: add a lazy-loaded **"lineage"** `<details>` per card, but
  **only render it when the discovery actually has a relation** — i.e. it
  participates in a SUPERSEDES edge (either direction) OR `related_to` is
  non-empty. Render `supersedes` and `superseded_by` as directed edges and put
  `related` in its own labelled sub-list. Colour the supersession edges stronger
  than the related list.

To decide whether to show the expander without a probe, thread a lightweight
flag through the `discoveries` accessor (currently dropped): `related_to`
non-empty is already known relationally; for supersession, expose a boolean
`has_supersession` (cheap: `status='superseded'` OR the id appears as a
SUPERSEDES endpoint). Otherwise the endpoint can return `{empty:true}` and the
expander hides after a probe (worse UX).

## Checks

- Focused endpoint test: insert temp `knowledge.discoveries` rows, create a
  `SUPERSEDES` edge (use `supersede_discovery`, the real path), assert
  `supersedes` / `superseded_by` / `related` resolve and are depth-bounded. Add a
  cycle / depth-cap case so a bad chain can't hang the read.
- `pytest tests/test_dashboard_redesign_route.py tests/test_dashboard.py`
- `cd dashboard && npm test && npm run lint && npm run format:check`
  (the `format:check` gate bit a prior dashboard PR — prettier the JS).
- Verify in a **real browser** on a discovery with a known supersedes chain
  (post-#991, once a real supersession has created an edge — query for a
  discovery that is the target of a `SUPERSEDES` edge, not merely
  `status='superseded'`).

## Caveats (don't paper over these)

- **Supersession is the only authored edge; `related_to` is similarity.** Keep
  them visually and semantically distinct. Show the edge meaning, don't collapse
  everything to "history."
- **Most discoveries have no lineage.** The expander must simply *not appear* —
  not show a "no lineage" row. Absence is silent (same rule as the rest of the
  dashboard).
- **Bound the walk.** AGE can't do safe variable-length paths; iterate a fixed
  small depth in code and guard against cycles.
- **Don't trust `response_to_id` / `response_type` / `provenance_chain`** — the
  first two are empty, the third was empty on every superseded row checked.

## Surfaces

- backend: `src/http_api.py` (endpoint + `register_http_routes`),
  `src/storage/knowledge_graph_age.py` (SUPERSEDES read shape at :2029), `tests/`
- frontend: `dashboard/redesign/sections/discoveries.js`, `dashboard/redesign/data.js`
- patterns to copy: agent-history endpoint (#935, `http_api.py:1025`),
  dialectic transcript lazy-load (#981)
