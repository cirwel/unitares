---
name: knowledge-graph
description: >
  Use when an agent needs to search the shared knowledge graph, contribute a discovery,
  or update existing entries. Covers search, tagging, discovery types, and status lifecycle.
last_verified: "2026-06-27"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/knowledge/handlers.py
  - unitares/src/knowledge_graph.py
  - unitares/src/storage/knowledge_graph_age.py
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

The runtime may still expose older search aliases, but prefer the unified `knowledge(action="search")` path when available. Duplicate entries fragment knowledge and make search less effective.

## Quick Contribution

For low-friction contributions, use the unified note action:

```
knowledge(
  action: "note",
  summary: "What you discovered or observed",
  tags: ["domain", "type", "context"]
)
```

Notes are automatically shared with all agents. Use this when you find something useful, spot a bug, or have an insight that others should know about. The legacy `leave_note()` tool remains as a compatibility alias, but it is deprecated.

## Full CRUD Operations

For more control, use the `knowledge()` tool with an action parameter:

| Action | Purpose |
|--------|---------|
| `store` | Create a new discovery with full metadata |
| `search` | Search by query, tags, or both |
| `get` | Get knowledge for a specific agent |
| `list` | List graph statistics or summary views |
| `update` | Modify an existing discovery (status, content, tags) |
| `details` | Get full details including graph relationships |
| `note` | Quick note storage through the unified interface |
| `cleanup` | Run lifecycle cleanup for stale entries |
| `synthesize` | Roll up a topic's discoveries into a summary row (see below) |
| `stats` | Get knowledge graph statistics |
| `supersede` | Mark a discovery as superseding another (creates a SUPERSEDES edge) |
| `audit` | Read-only staleness/health scoring of open entries |

## Discovery Types

When storing a discovery, classify it:

| Type | When to Use |
|------|-------------|
| `note` | General observation, context, or reminder |
| `insight` | Understanding gained from analysis or pattern recognition |
| `bug_found` | A bug or defect you identified |
| `improvement` | A suggestion for how something could be better |
| `pattern` | A recurring pattern you noticed across multiple instances |

Check the live tool schema if you are unsure which enum values the current runtime accepts. Do not invent discovery types casually.

## Status Lifecycle

Every discovery has a status:

```
open  -->  resolved    (problem solved, finding addressed)
  \-->  archived     (no longer relevant, superseded)
```

- **open**: Active, still relevant, may need attention
- **resolved**: The issue or finding has been addressed
- **archived**: No longer relevant (outdated or duplicate)

The runtime accepts more terminal states than the two above: `disputed` (contested finding), `closed` (terminal, generic), `wont_fix` (acknowledged, deliberately not addressed), and `superseded` (replaced by a newer entry — pair with `knowledge(action="supersede")`). Check the live tool schema for the authoritative set.

## Tagging Best Practices

Tags are how future agents find your contributions. Be intentional:

- **Include the domain**: `identity`, `database`, `performance`, `deployment`, `testing`
- **Include the type**: `bug`, `insight`, `pattern`, `config`, `dependency`
- **Include context**: `postgres`, `eisv`, `dialectic`, `discord-bridge`
- **Be specific**: `pool-connection-leak` is more useful than `bug`
- **Be consistent**: Check existing tags before inventing new ones

## Closing the Loop

The graph accumulates knowledge well but does not close loops automatically. This is a known gap that every agent should help address:

- **When you resolve something, update its status.** Do not leave it as `open`.
- **When you find a duplicate, archive the less complete one** and reference the better entry.
- **When a finding is outdated, archive it** with a note about what superseded it.
- **Periodically check for stale entries** in your domain using `knowledge(action="cleanup")`.

Unresolved entries create noise. Closed loops create trust in the graph.

## Synthesis: rolling up topics

`knowledge(action="synthesize")` compounds the discrete discoveries under a topic
(a tag) into a single rolled-up **summary row**, so a cross-referenced, compounded
narrative exists *before* query time instead of only being assembled on read via
`search(..., synthesize=true)`. It is the GraphRAG "community summary" pattern: a
hierarchical summary layer maintained over the base discovery nodes.

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
