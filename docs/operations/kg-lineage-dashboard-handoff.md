# Handoff: KG supersession / correction lineage on the dashboard

**Goal:** let an operator click a knowledge-graph discovery and see its
**lineage** — what it supersedes / corrects / extends, and what later
superseded or corrected *it*. This is the "history" the dashboard currently
can't show for the KG (dialectic transcripts and agent EISV history already
ship; this is the missing third).

This is an **exposure** task, not a graph-modeling one. The lineage already
exists relationally; the read API just doesn't return it.

## Why it isn't done yet

The redesign discovery cards (`dashboard/redesign/sections/discoveries.js`)
already surface summary, provenance (agent / originating session / system
version / created), details, and tags. But the *links between* discoveries are
invisible because `knowledge(action="search")` strips them from its result
(the response carries `id, by, summary, session_id_at_write, type, status,
tags, created_at, details, _agent_id, system_version` — no relation fields).

## The data model (verified)

`knowledge.discoveries` records lineage relationally:

| Column | Meaning |
|---|---|
| `response_to_id` (text) | the discovery this one responds to — the parent link in a chain |
| `response_type` (text, CHECK enum) | the **edge label**: `extend` / `question` / `disagree` / `support` / `answer` / `follow_up` / `correction` / `elaboration` / **`supersedes`** |
| `related_to` (text[]) | sibling / related discovery ids |
| `provenance_chain` (jsonb-ish — *verify shape*) | ancestry chain |
| `epoch`, `provenance` | additional context |

So a discovery's lineage is:
- **ancestors** — walk `response_to_id` upward (bounded depth);
- **descendants** — discoveries whose `response_to_id` = this id (reverse lookup, bounded/transitive);
- **edges** — labelled by each row's `response_type`;
- **related** — the `related_to` array.

AGE graph has **no** supersession label (checked: `ag_label` has nothing
matching `%supersed%`). Use the relational columns, not AGE.

## Backend (recommended)

Add a read-only endpoint mirroring the agent-history one already in the repo —
`http_agent_history` in `src/http_api.py` (route `/v1/agents/{agent_id}/history`,
added in PR #935) is the exact template: bearer-auth via `_check_http_auth`,
`JSONResponse`, graceful-empty, registered in `register_http_routes`.

```
GET /v1/discoveries/{id}/lineage
```

returns:

```json
{
  "id": "<id>",
  "ancestors":   [{"id","summary","type","response_type","created_at","by"}],
  "descendants": [{"id","summary","type","response_type","created_at","by"}],
  "related":     [{"id","summary","type"}]
}
```

SQL sketch (recursive CTE up for ancestors; one join down for descendants):

```sql
WITH RECURSIVE up AS (
  SELECT d.id, d.summary, d.type, d.response_type, d.created_at, d.agent_id, 1 AS depth
  FROM knowledge.discoveries d
  WHERE d.id = (SELECT response_to_id FROM knowledge.discoveries WHERE id = $1)
  UNION ALL
  SELECT p.id, p.summary, p.type, p.response_type, p.created_at, p.agent_id, up.depth + 1
  FROM knowledge.discoveries p
  JOIN up ON p.id = (SELECT response_to_id FROM knowledge.discoveries WHERE id = up.id)
  WHERE up.depth < 20
)
SELECT * FROM up;
-- descendants: SELECT ... FROM knowledge.discoveries WHERE response_to_id = $1 (+ recurse, bounded)
-- related:     SELECT ... WHERE id = ANY((SELECT related_to FROM knowledge.discoveries WHERE id = $1))
```

(Alternative: expose `response_to_id` / `response_type` / `related_to` in the
existing `knowledge` search result and resolve client-side. Simpler to wire but
N+1 for summaries and no transitive walk — the dedicated endpoint is cleaner.)

## Frontend

Reuse the **dialectic-transcript lazy-load** pattern shipped in PR #981 — it is
the model to copy:
- `dashboard/redesign/sections/dialectic.js` → `renderTranscript()` + the
  `<details class="dlc-transcript">` toggle that lazy-fetches on first expand.
- `dashboard/redesign/data.js` → `dialecticSession(id)` accessor.

Do the same here:
- `data.js`: add `discoveryLineage(id)` → calls the new endpoint, `withFallback`.
- `discoveries.js`: add a lazy-loaded **"lineage"** `<details>` per card, but
  **only render it when the discovery actually has a relation** (it has a
  `response_to_id`, OR it is some row's `response_to_id`, OR `related_to` is
  non-empty). Render the chain as directed edges: `supersedes → <summary>`,
  `superseded by ← <summary> (correction)`, plus a related list. Colour the
  `supersedes` edge stronger than the soft ones.

You'll need the discovery's relation flags in the list to decide whether to show
the expander — thread `response_to_id` / `has_responses` / `related_to` through
the `discoveries()` accessor (currently dropped), or have the lineage endpoint
return `{empty:true}` and hide the expander after a probe (worse UX).

## Checks

- Focused endpoint test (temp `knowledge.discoveries` rows with a
  `response_to_id` chain + a `supersedes` edge; assert ancestors/descendants/
  related resolve, depth-bounded).
- `pytest tests/test_dashboard_redesign_route.py tests/test_dashboard.py`
- `cd dashboard && npm test` and `npm run lint` + `npm run format:check`
  (the `format:check` gate bit the automation PR — prettier the JS).
- Verify in a **real browser** on a discovery with a known supersedes chain.

## Caveats (don't paper over these)

- `supersedes` is the strong edge; `correction` / `extend` / `disagree` etc. are
  *softer* relations. Show the edge label — do **not** collapse everything to
  "history."
- Bound the recursive walk (chains can be long; guard against cycles — a bad
  `response_to_id` loop will hang the CTE without the depth cap).
- **Most discoveries have no lineage** (`response_to_id` null, never referenced).
  The expander must simply *not appear* — not show "no lineage." (Same honesty
  rule as the rest of the dashboard: absence is silent, not a false row.)
- `provenance_chain` shape is unverified — inspect a populated row before relying
  on it; `response_to_id` + `response_type` are the load-bearing fields.

## Surfaces

- backend: `src/http_api.py` (endpoint + `register_http_routes`), `tests/`
- frontend: `dashboard/redesign/sections/discoveries.js`, `dashboard/redesign/data.js`
- patterns to copy: agent-history endpoint (#935, `/v1/agents/{id}/history`),
  dialectic transcript lazy-load (#981)
