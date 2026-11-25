---
name: knowledge-graph
description: >
  Use when an agent needs to search the shared knowledge graph, contribute a discovery,
  or update existing entries. Covers search, tagging, discovery types, and status lifecycle.
last_verified: "2026-10-16"
freshness_days: 21
source_files:
  - unitares/src/knowledge_notices.py
  - unitares/db/postgres/migrations/074_knowledge_change_notices.sql
  - unitares/src/knowledge_authority.py
  - unitares/src/mcp_handlers/knowledge/handlers.py
  - unitares/src/mcp_handlers/knowledge/read_metadata.py
  - unitares/src/mcp_handlers/knowledge/synthesis.py
  - unitares/src/mcp_handlers/schemas/knowledge.py
  - unitares/src/alias_schema.py
  - unitares/src/mcp_handlers/consolidated.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/tool_descriptions.json
  - unitares/src/mcp_handlers/support/param_normalization.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_MEMORY_SUGGESTION_LIMIT
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_SEARCH_MORE_ROW_LIMIT
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_memory_suggestions
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_append_more_results
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_enforce_search_projection_budget
  - unitares/src/mcp_handlers/middleware/envelope_step.py#_note_stale_digests
  - unitares/src/knowledge_graph.py
  - unitares/src/knowledge_graph_lifecycle.py
  - unitares/src/storage/knowledge_graph_age.py
  - unitares/src/storage/knowledge_graph_postgres.py
  - unitares/src/db/mixins/knowledge_graph.py
  - unitares/skills/knowledge-graph/references/authority-and-synthesis.md
  - unitares/skills/knowledge-graph/references/search-and-notices.md
---

# Knowledge Graph

## What It Is

Shared institutional memory across all agents, backed by PostgreSQL (canonical
rows) with Apache AGE (graph edges under `UNITARES_KNOWLEDGE_BACKEND=age`,
required by the server either way; native installs default to `postgres`
full-text search). Discoveries persist across sessions for every agent.

## Search Before Creating

Always search before adding new entries to avoid duplicating knowledge:

```
search_shared_memory(query="description of what you're looking for", tags=["relevant", "tags"], limit=10)
```

- `search_shared_memory` is the lean workflow alias for `knowledge(action="search")`
  (3 digests + up to 17 `more_results` summaries). Open any hit with
  `use_tool(tool_name="knowledge", arguments={"action": "details", "discovery_id": ...})`,
  or pass `response_mode="full"` for inline details.
- **Archived/cold visibility**: `archived` and `cold` rows are hidden by
  default unless fewer than 3 other rows match (triggering an optional halved-score
  fallback pass) or you pass `include_archived=true` / `include_cold=true` or
  `status=...` — do not conclude "not found" without checking when searching
  historical topics.
- **Authority precedence**: default search is authority-aware — imported memory
  (`source-<provider>-memory`, `authority.tier="imported_context"`,
  `trust="unreviewed"`) is down-ranked in close contests against `native_finding`
  and evidence-linked `governed_claim` rows, and omitted from queryless listings.
- **Freshness controls**: use `sort_by="created_at"` (FTS only, not semantic/hybrid)
  or `created_after`/`created_before` (ISO 8601) for newest-first reads, or
  `recency_half_life_days` to weight relevance by age.
- **Verify before relying**: before treating a finding as an operating rule,
  open its full `details`, inspect `superseded_by` and `resolution_notes`, and
  check canonical evidence. A summary or recent timestamp is not verification.

When a search returns `more_results`, `archived_cold_fallback`, `imported_omitted`,
`resolution_notes` offsets, or `staleness_warning`, read
`references/search-and-notices.md`
(remote MCP: `skills(name="knowledge-graph", reference="search-and-notices.md")`).

## Quick Contribution and CRUD

Prefer `store_finding` / `update_finding` for structured work, or
`knowledge(action="note")` / `leave_note()` for quick notes:

```
store_finding(
  summary="What was found",
  discovery_type="bug_found",
  details="Evidence and impact",
  tags=["domain", "component"]
)
```

- When checked evidence changes a finding's conclusion, update `summary` to the
  current claim and preserve prior history in `details` (`resolution_notes`
  alone leaves the stale headline in search). Never rewrite a historical or
  superseded record; link its replacement instead.
- To reply to another finding, pass `response_to={"discovery_id": ..., "response_type": ...}`.

| Action (`knowledge(action=...)`) | Purpose |
|---|---|
| `store` / `update` / `note` | Create (`store_finding`), modify (`update_finding`), or leave a quick note |
| `search` / `get` / `details` | Search (`search_shared_memory`), fetch one agent's rows or ID, or paginate full `details` (`offset`, `length` default 2000; `include_response_chain=true` on AGE) |
| `list` / `stats` / `audit` | Raw counts, lifecycle buckets, or read-only staleness audit (`scope`: `open`\|`all`\|`by_agent`) |
| `supersede` | Create a `SUPERSEDES` edge (`discovery_id` → `supersedes_id`, AGE backend only) and mark older `superseded` |
| `promote` | Create a `governed_claim` from an imported memory + non-memory `evidence_ids` + `verification_basis` + `decision_standard` |
| `synthesize` / `cleanup` | Roll up a topic tag into a `topic_rollup`, or run lifecycle archival passes (`dry_run` defaults true; never deletes) |
| `notices` | Acknowledge received `knowledge_changes` revision UUIDs or mute/unmute delivery |

## Discovery Types

| Group | Types |
|------|-------|
| Decisions and rules | `architectural_decision`, `rule` |
| Learnings and analysis | `learning`, `insight`, `pattern`, `exploration`, `observation`, `experiment` |
| Engineering changes | `bug_found`, `bug_fix`, `refactoring`, `improvement`, `documentation` |
| Discussion and general memory | `question`, `note` |

`bug` normalizes to `bug_found`. `topic_rollup` is system-generated by synthesis
— do not use it for ordinary stores.

## Status Lifecycle and Identity Rules

```
open  -->  resolved / closed / wont_fix
  |\-->  archived / superseded
  \-->  disputed
```

`cold` is system-managed deep archive. High/critical rows enforce ownership:
non-owners may close them only via `resolved`, `closed`, or `wont_fix`.

- **Always pass `client_session_id` on writes** (omit bare `agent_id`). Under
  strict identity, passing `agent_id` without a proved binding is refused
  (`identity_required`); server-inferred bindings cannot write high/critical
  rows or call `supersede`, `cleanup`, `synthesize`, `promote`, or `audit`.

## Tagging Best Practices

- Include domain (`identity`, `database`, `deployment`), type (`bug`, `insight`,
  `pattern`), and specific context (`pool-connection-leak` over generic `bug`).
- Caps: max 50 tags, max 128 characters per tag after normalization.
- **Lifecycle tags**: `ephemeral`, `temp`, `scratch`, `test`, `demo` archive
  after 7 days — never tag a durable finding about tests with `test`.
  `permanent`, `foundational`, `architecture`, `decision` (and types
  `architectural_decision`, `learning`, `pattern`) prevent auto-archival
  (permanence wins on tie). `store(..., supersedes=<older>)` refuses only when
  the older row carries a permanent **tag** (`knowledge(action="supersede")`
  does not check tags).

## Closing the Loop

When closing a finding (`resolved`, `closed`, `wont_fix`, `superseded`), pass
`resolution_notes`, `closure_class`, and `closure_evidence`:

- `closure_class`: `fix_verified` (evidence: `deployed`, `observed`),
  `unobserved` (evidence: `window`, `instrument_check`), `not_reproducible`,
  `obsolete`, or `duplicate`.
- Keep evidence values short pointers (commits, build SHAs, queries; 8 KiB JSON
  cap; 96 KiB details cap). Omitted fields on `update_finding` are preserved.
- Reopening (`open`/`disputed`) clears `closure_class` and `closure_evidence`.
- Periodically audit open debt with `knowledge(action="audit")` and preview
  cleanup with `knowledge(action="cleanup")`.

## Authority, Synthesis, and Change Notices (Quick Pointers)

- **Promoting imported memory**: imported rows are context, not policy; use
  `knowledge(action="promote")` with independent non-memory evidence to mint an
  attributable `governed_claim` with `provenance.transition_receipt`. When
  promoting, verifying transition receipts, or running topic synthesis
  (`topic_rollup` / `consolidation_hint`), read
  `references/authority-and-synthesis.md`
  (remote MCP: `skills(name="knowledge-graph", reference="authority-and-synthesis.md")`).
- **Active-session change notices**: successful tool calls may include
  unprompted `knowledge_changes` (up to 3 coalesced row/revision pointers for
  your proved UUID). Read details before acting, and acknowledge exact revision
  UUIDs via `knowledge(action="notices", acknowledge=[...])`. When handling
  `knowledge_changes`, `gap_token`, `resolution_notes` offsets, or closure/staleness
  edge cases, read `references/search-and-notices.md`
  (remote MCP: `skills(name="knowledge-graph", reference="search-and-notices.md")`).
