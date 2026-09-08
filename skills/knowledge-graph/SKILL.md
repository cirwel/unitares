---
name: knowledge-graph
description: >
  Use when an agent needs to search the shared knowledge graph, contribute a discovery,
  or update existing entries. Covers search, tagging, discovery types, and status lifecycle.
last_verified: "2026-09-08"
freshness_days: 21
source_files:
  - unitares/src/mcp_handlers/knowledge/handlers.py
  - unitares/src/mcp_handlers/knowledge/synthesis.py
  - unitares/src/mcp_handlers/schemas/knowledge.py
  - unitares/src/mcp_handlers/consolidated.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/support/param_normalization.py
  - unitares/src/knowledge_graph.py
  - unitares/src/knowledge_graph_lifecycle.py
  - unitares/src/storage/knowledge_graph_age.py
  - unitares/src/storage/knowledge_graph_postgres.py
  - unitares/src/db/mixins/knowledge_graph.py
source_digests:
  unitares/src/mcp_handlers/knowledge/handlers.py: "9ddb3b9a52bfd79b"
  unitares/src/mcp_handlers/knowledge/synthesis.py: "f33e76c5d5364ce9"
  unitares/src/mcp_handlers/schemas/knowledge.py: "f2b27e8116d9d91d"
  unitares/src/mcp_handlers/consolidated.py: "1dbe503c218ad89f"
  unitares/src/mcp_handlers/tool_stability.py: "9513b90cde7e6601"
  unitares/src/mcp_handlers/support/param_normalization.py: "6e16db988efa1d45"
  unitares/src/knowledge_graph.py: "0f53dddc433c13aa"
  unitares/src/knowledge_graph_lifecycle.py: "b2988b7694718525"
  unitares/src/storage/knowledge_graph_age.py: "0541b46146c6084c"
  unitares/src/storage/knowledge_graph_postgres.py: "212a048e391c53b3"
  unitares/src/db/mixins/knowledge_graph.py: "f3f00b0381c5fa10"
---

# Knowledge Graph

## What It Is

The knowledge graph is shared institutional memory across all agents. It is backed by PostgreSQL — full-text search is the canonical default backend, with Apache AGE as an optional graph backend (`UNITARES_KNOWLEDGE_BACKEND=age`). Every agent can search, contribute, and update entries. Discoveries persist across sessions and are available to all agents in the system.

## Search Before Creating

Always search before adding new entries:

```
knowledge(
  action: "search",
  query: "description of what you're looking for",
  tags: ["relevant", "tags"],
  limit: 10
)
```

`search_shared_memory(query=...)` is the discoverable workflow alias for the
same handler (`knowledge(action="search")`), but it defaults `response_mode` to
`lean` and forces `include_details=false`; pass `response_mode="full"` for the
router's inline-detail behaviour. Use either it or the unified router;
duplicate entries fragment knowledge and make search less effective.

You may omit `query` entirely when filtering by `tags`, `discovery_type`,
`severity`, `status`, or `agent_id`. Date filters (`created_after` /
`created_before`) appear in the schema but are not honoured by the search
handler. A supplied-but-blank query is rejected so a caller mistake cannot turn
into an accidental broad scan. Omit `include_details` to let the server expand a
small result set (up to 3 hits) automatically; pass `include_details=false` when
summaries only are intentional.

## Quick Contribution

For low-friction contributions, use the unified note action:

```
knowledge(
  action: "note",
  summary: "What you discovered or observed",
  tags: ["domain", "type", "context"]
)
```

Notes are automatically shared with all agents. Use this when you find something useful, spot a bug, or have an insight that others should know about. `leave_note()` is also supported as a lower-friction entry point to the same handler (kept by operator decision, 2026-08-29; it is not deprecated).

For a structured discovery, prefer the dedicated workflow alias:

```
store_finding(
  summary: "What was found",
  discovery_type: "bug_found",
  details: "Evidence and impact",
  tags: ["domain", "component"]
)
```

Use `update_finding(discovery_id=..., ...)` to revise or close it. These aliases
route to `knowledge(action="store")` and `knowledge(action="update")`; they do
not create a second store.

## Full CRUD Operations

For more control, use the `knowledge()` tool with an action parameter:

| Action | Purpose |
|--------|---------|
| `store` | Create a new discovery with full metadata |
| `search` | Search by query, tags, or both |
| `get` | Get one agent's knowledge, or read back a single `discovery_id` |
| `list` | Raw status aggregate (`epoch_scope`, `including_cold`); its numbers differ from `stats` by design |
| `update` | Modify an existing discovery (status, content, tags) |
| `details` | Full row with `details` pagination (`offset`, `length` default 2000); `include_response_chain=true` adds the typed response chain (AGE backend only) |
| `note` | Quick note storage through the unified interface |
| `cleanup` | Run the lifecycle passes graph-wide (tag canonicalization; `ephemeral`-tagged → archived after 7 days; resolved → archived after 30 days, permanent entries skipped; archived → cold after 90 days). Never deletes; `dry_run` defaults to true |
| `synthesize` | Roll up a topic's discoveries into a summary row (see below) |
| `stats` | Lifecycle-bucket statistics |
| `supersede` | Create a SUPERSEDES edge from `discovery_id` (newer) to `supersedes_id` (older) and flip the older row to `superseded` — AGE backend only; on the default Postgres backend it returns an error |
| `audit` | Read-only staleness/health scoring (`scope` open \| all \| by_agent, `top_n` default 10) |

## Discovery Types

When storing a discovery, classify it. The current caller-facing set is:

| Group | Types |
|------|-------|
| Decisions and rules | `architectural_decision`, `rule` |
| Learnings and analysis | `learning`, `insight`, `pattern`, `exploration`, `observation`, `experiment` |
| Engineering changes | `bug_found`, `bug_fix`, `refactoring`, `improvement`, `documentation` |
| Discussion and general memory | `question`, `note` |

`bug` is accepted as a compatibility spelling and normalizes to `bug_found`.
`topic_rollup` is system-generated by synthesis and may be searched, but agents
should not use it for ordinary stores. Check the live tool schema if you are
unsure; do not invent discovery types casually.

## Status Lifecycle

Every discovery has a status:

```
open  -->  resolved / closed / wont_fix
  |\-->  archived / superseded
  \-->  disputed
```

- **open**: Active, still relevant, may need attention
- **resolved**: The issue or finding has been addressed
- **archived**: No longer relevant (outdated or duplicate)
- **superseded**: Replaced by a newer entry. Make the link explicit with
  `knowledge(action="supersede", discovery_id=<newer>, supersedes_id=<older>)`
  on the AGE backend; on the default Postgres backend use
  `update(status="superseded", superseded_by=<newer>)` or
  `store(..., supersedes=<older>)` and expect a `supersession_warning` that the
  edge was not recorded
- **disputed**: Contested and still worth retaining
- **closed / wont_fix**: Terminal generic closure / deliberate non-action

The diagram is advisory: the server checks membership in the status set, not a
transition matrix. `cold` is the lifecycle deep-archive state and is normally
system-managed, not a routine agent transition. High/critical discoveries have
stricter identity and ownership rules; a non-owner may close one only through
the allowed terminal status path (`resolved`, `closed`, `wont_fix`).

## Tagging Best Practices

Tags are how future agents find your contributions. Be intentional:

- **Include the domain**: `identity`, `database`, `performance`, `deployment`, `testing`
- **Include the type**: `bug`, `insight`, `pattern`, `config`, `dependency`
- **Include context**: `postgres`, `eisv`, `dialectic`, `discord-bridge`
- **Be specific**: `pool-connection-leak` is more useful than `bug`
- **Be consistent**: Check existing tags before inventing new ones
- **Mind the lifecycle tags**: `ephemeral`, `temp`, `scratch`, `test`, `demo`
  archive the entry after 7 days; `permanent`, `foundational`, `architecture`,
  `decision` (and the `learning` / `pattern` types) make it permanent, and
  permanence wins on tie. A durable finding *about* the test suite must not
  carry the `test` tag.

## Closing the Loop

The graph accumulates knowledge well but does not close loops automatically. This is a known gap that every agent should help address:

- **When you resolve something, update its status and add
  `resolution_notes`.** Omitted fields are preserved; do not resend stale
  content just to close the row. When the new status is a closing one
  (`resolved`, `closed`, `wont_fix`, `superseded`), also pass `closure_class` —
  `fix_verified` | `unobserved` | `not_reproducible` | `obsolete` | `duplicate`
  — with `closure_evidence` (`{deployed, observed}` for `fix_verified`,
  `{window, instrument_check}` for `unobserved`). A closure without one is
  accepted but flagged `closure_class: null` with a `closure_class_note`.
- **When you find a duplicate, archive the less complete one** and reference the better entry.
- **When a finding is outdated, archive it** with a note about what superseded it.
- **Periodically audit stale open entries** with `knowledge(action="audit")`
  (read-only), and run `knowledge(action="cleanup")` (dry-run by default) to
  apply the lifecycle archival passes. Cleanup has no domain or tag scope and
  never touches open entries; staleness scoring is `audit`'s job.

Unresolved entries create noise. Closed loops create trust in the graph.
Open-entry staleness warnings use the latest write (`updated_at` when present),
not merely the original creation time, so a genuinely maintained finding does
not age as if untouched.

## Synthesis: rolling up topics

`knowledge(action="synthesize")` compounds the discrete discoveries under a topic
(a tag) into a single rolled-up **summary row**, so a cross-referenced, compounded
narrative exists *before* query time instead of only being assembled on read via
`search(..., synthesize=true)` (an unadvertised parameter that the handler
honours). It is the GraphRAG "community summary" pattern: a hierarchical summary
layer maintained over the base discovery nodes.

- `knowledge(action="synthesize")` — sweep the densest topics and (re)build their
  rollups. `topic="..."` rolls up a single tag; `dry_run=true` previews without
  writing; `min_members` (default 3) sets the threshold; `use_llm=false` forces the
  deterministic narrative.
- Rollups are stored as ordinary discoveries (`type="topic_rollup"`, deterministic
  id `rollup::<topic>`, tagged `rollup`), so they upsert in place across runs and
  are found by normal search — e.g. `knowledge(action="search", tags=["rollup"])`.

Run it **on demand or on a periodic cadence (like cleanup/lint), not on every
write** — a per-write LLM pass across a multi-agent fleet is exactly the
high-frequency-noise anti-pattern this graph avoids.

## Deferred: bi-temporal fact validity (documented idea, not built)

A first-class notion of *when a fact became true/false* (valid-from / valid-to
plus observation time, per the Graphiti/Zep bi-temporal model) was considered and
**deliberately deferred**. It is a substrate-level change — migration, AGE query
rewrites, and every read path having to reason about time — for a payoff
(point-in-time reconstruction, automatic invalidation) that is speculative for
current usage. The existing `superseded` status + `created_at` is an ~80%
substitute. The signal to build it is a concrete failure: an agent acting on a
stale fact in a way that actually bites. Until then this stays a documented idea,
not a roadmap item.
