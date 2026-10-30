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
---

# Knowledge Graph

## What It Is

The knowledge graph is shared institutional memory across all agents. It is backed by PostgreSQL: the relational rows are the canonical store. The Docker quickstart runs the AGE backend (`UNITARES_KNOWLEDGE_BACKEND=age`), which also writes the graph over those rows; a native install with the variable unset resolves to the `postgres` backend, which keeps the rows and full-text search without the graph. The AGE extension itself is required by the server either way. Every agent can search, contribute, and update entries. Discoveries persist across sessions and are available to all agents in the system.

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

The `lean` and `compact` tiers show three digests whatever `limit` asked for.
The same response lists the results after those three under `more_results`,
one row each (`discovery_id`, `type`, `status`, a short summary), up to 17
rows; `state_summary.more_results_shown` counts them. Pass `limit` of 3 or
less for the three digests alone. Open any row with
`knowledge(action="details", discovery_id=...)`; on the progressive surface
that router is reached as
`use_tool(tool_name="knowledge", arguments={"action": "details", "discovery_id": ...})`,
which is the form the response hints name. To record a finding as a reply
to another, pass `response_to={"discovery_id": ..., "response_type": ...}` to
`store_finding`; the ack's `state_summary.response_to` confirms the link. When the digests carry scores on
more than one `relevance_basis`, `state_summary.relevance_note` says they are
not comparable; the list order is the ranking.

Lean reads keep the result tier, an `open_one` route and any disclosure that an
explicit `include_details=true` request was overridden. Repeated mode tutorials
and serialization diagnostics live in `compact`; `full` retains the canonical
payload. The response budget is a ceiling, so spare room need not be filled with
coaching. Confidence/degradation caveats, author identity, imported-memory
warnings, supersession and appended-note pointers retain their existing budget
and omission-marker rules.

For a small working context, search first, open the few relevant records by ID,
and use their note offsets or pagination when needed. Keep the decision, its
scope and caveats, and evidence IDs in the working summary; retrieve the original
record when the evidence must be checked. A summary is not a replacement for
its source. After processing delivered change notices, acknowledge their exact
revision IDs through `knowledge(action="notices", acknowledge=[...])` so the
same receipts stop repeating. Do not acknowledge unseen revisions or mute
delivery merely to reduce context.

You may omit `query` entirely when filtering by `tags`, `discovery_type`,
`severity`, `status`, or `agent_id_filter` (author UUID). The explicit
`agent_id_filter` takes precedence; `agent_id` remains a fallback for existing
callers. A blank explicit filter is rejected, and surrounding whitespace is
trimmed. Search also supports `include_provenance`;
request `response_mode="full"` when you need the full result fields. The search
alias omits controls for other actions, such as closure evidence and synthesis;
use `update_finding` or the corresponding `knowledge` action for those tasks.
A supplied-but-blank query is rejected so a caller mistake cannot turn
into an accidental broad scan. Omit `include_details` to let the server expand a
small result set (up to 3 hits) automatically; pass `include_details=false` when
summaries only are intentional.

### Newest first, and "what is new since T"

Relevance order can bury an entry written this morning under older, better
matches. Two controls answer "what is new":

- `sort_by="created_at"` returns the query's full-text matches newest first.
  The database orders the match set, so a fresh entry comes first however
  weakly it ranks. It cannot be combined with `search_mode="semantic"` or
  `"hybrid"`, or with `semantic=true`: similarity has no match boundary to
  order within. An entry that shares no query terms (after stemming) is not a
  match, and a multi-term query needs every term unless the AND search found
  nothing, in which case it falls back to OR.
- `created_after` / `created_before` take ISO 8601 timestamps (UTC when no
  offset is given; `"2026-09-26"` is midnight UTC) and are exclusive. They work
  with any order and any search mode. With no query, `created_after` alone is
  the "what is new since T" read, newest first:
  `search_shared_memory(created_after="2026-09-26T00:00:00Z")`.

- `recency_half_life_days` keeps relevance order but weights it by age: each
  result's score is multiplied by `0.5 ** (age_days / N)`. Off by default; it
  needs a query and cannot be combined with `sort_by="created_at"`. Use it when
  both relevance and freshness matter, and `sort_by` when only freshness does.

An unparseable or inverted window is refused, not ignored. The response echoes
`sort_by`, the window and `recency_half_life_days` when they differ from the
default. The default order is still relevance.

Default search is authority-aware. Imported memory rows remain searchable, but
their `authority.tier="imported_context"` marker down-ranks them in close
relevance contests against native findings and evidence-linked governed claims.
This is a retrieval preference, not a truth verdict. Pass `authority_mode="all"`
to preserve raw backend order, or filter by a `source-<provider>-memory` tag to inspect that
lane directly. Newest-first reads (`sort_by="created_at"`, or a queryless date
window) apply no authority reorder: time order is what they asked for.

A queryless listing (no `query`, for example a `status="open"` sweep) leaves
unpromoted imported memory out by default, because with nothing to match there
is no contest for the down-rank to decide and imports would fill the page. The
response reports `imported_omitted` with a hint; the same two routes read them
(`authority_mode="all"`, or a `source-<provider>-memory` tag filter). Check-in memory
suggestions leave them out as well. A query search still returns them.

### Archived and cold rows on a short page

Search hides `archived` and `cold` rows unless you pass `include_archived` /
`include_cold` or a `status` filter. Lifecycle aging moves rows there by age
alone, so a query search that finds fewer than 3 other rows runs a second pass
that includes them. Rows from that pass come after every other row, carry
`retrieval_tier="archived_cold_fallback"`, and their reported scores are
halved. The response's `archived_cold_fallback` block (lean:
`state_summary.archived_cold_fallback`) gives the counts. Check `status` and
`superseded_by` before relying on such a row. The pass is optional: when it
fails or does not fit in what is left of the search's time budget, the page
holds the first pass's rows alone and `archived_cold_fallback_error` (lean:
`state_summary.archived_cold_fallback_error`) says why; pass
`include_archived=true` / `include_cold=true` to ask for those rows directly. An explicit
`include_archived=false` or `include_cold=false` keeps that status out.
Queryless listings and searches with a `status` filter never get the second
pass.

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
| `update` | Modify an existing discovery (status, content, tags); a `summary` over 4,003 characters, the most a store keeps, is refused |
| `details` | Full row with `details` pagination (`offset`, `length` default 2000); `include_response_chain=true` adds the typed response chain on the AGE backend, bounded by `max_chain_depth` (default 10, maximum 32), `response_chain_limit` (default 100, maximum 200), and `response_chain_offset` (default 0) |
| `note` | Quick note storage through the unified interface |
| `cleanup` | Run the lifecycle passes graph-wide (tag canonicalization; `ephemeral`-tagged → archived after 7 days; resolved → archived after 30 days, permanent entries skipped; archived → cold after 90 days). Never deletes; `dry_run` defaults to true |
| `synthesize` | Roll up a topic's discoveries into a summary row (see below) |
| `stats` | Lifecycle-bucket statistics |
| `supersede` | Create a SUPERSEDES edge from `discovery_id` (newer) to `supersedes_id` (older) and flip the older row to `superseded` — AGE backend only; on the default Postgres backend it returns an error. `resolution_notes` are appended to the older row's details as `update` appends them, within the same bound; on a high or critical row only its owner may send them |
| `promote` | Create a governed claim from an imported-memory `discovery_id`, one or more non-memory `evidence_ids`, an explicit `verification_basis`, and a `decision_standard`; the source remains unchanged |
| `audit` | Read-only staleness/health scoring (`scope` open \| all \| by_agent, `top_n` default 10) |

## Memory authority and promotion

Imported memory is context, not policy. The harness-neutral
`source-<provider>-memory` tag pattern (for example `source-claude-memory`)
classifies a row as `imported_context`. Topic words such as `memory-sync` or
`imported-memory` do not: a finding about memory sync is not an import.
Agent-to-agent channel messages (a `channel-<topic>` tag
together with a `to-<agent>` tag, or a `[channel:<topic>]` summary prefix) are
`channel_message` and get the same close-contest down-rank. A tag filter made
only of `source-<provider>-memory` tags reads that lane in its own order. A `channel-*` tag
filter lifts the down-rank only from channel messages on that lane; every other
row keeps its authority order, since a `channel-` tag can be an ordinary topic. Normal stores are `native_finding`.
A row becomes
`governed_claim` only through `knowledge(action="promote")`, which adds a
server-authored receipt that ordinary store arguments cannot forge.

```
knowledge(
  action: "promote",
  discovery_id: "<imported-memory-id>",
  evidence_ids: ["<native-finding-id>"],
  summary: "Bounded claim accepted after verification",
  verification_basis: "Repository configuration and integration test agree",
  decision_standard: "At least one non-memory artifact independently supports it"
)
```

The promotion path requires a caller-owned registered identity, refuses to let
the source corroborate itself, and refuses imported memories as independent
evidence. It creates a new row linked to the source and evidence; it does not
rewrite or delete the imported memory. “Governed” means the transition is
attributable and evidence-linked, not that the resulting claim is infallible.

The created row also stores `provenance.transition_receipt`, and the promotion
response returns that object alongside the existing policy receipt. This v1
receipt is an unsigned, content-addressed statement inside the UNITARES
server's existing authority domain: it covers the exact accepted creation
projection and names which fields the server did and did not verify. It does
not prove claim correctness, independent review, operator approval or external
non-repudiation, and its empty `authorizes` list grants no authority. Verify it
against the durable row before relying on it; a later row edit intentionally
produces `subject_hash_mismatch` rather than rewriting the historical receipt.
Verification needs the complete covered row, including the full `details`
value. Summary-mode and paginated fragments fail explicitly as an incomplete
subject projection; retrieve and assemble all details pages before verifying.

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
  edge was not recorded. Where the edge exists, a search result or a `details`
  read of the older entry names its replacement in `superseded_by` (the
  default `search_shared_memory` digest carries the first successor id). A
  search that has spent its time budget still flags the row
  `superseded: true` but may omit `superseded_by`, so read the row to get it
- **disputed**: Contested and still worth retaining
- **closed / wont_fix**: Terminal generic closure / deliberate non-action

The diagram is advisory: the server checks membership in the status set, not a
transition matrix. `cold` is the lifecycle deep-archive state and is normally
system-managed, not a routine agent transition. High/critical discoveries have
stricter identity and ownership rules; a non-owner may close one only through
the allowed terminal status path (`resolved`, `closed`, `wont_fix`).

A write whose binding the server only inferred (`proof_origin:
"server_inferred"`: a fingerprint or pin match, not your `client_session_id`)
is not credited to the agent it matched. A low or medium store, note or update
is written under the anonymous writer id (`anonkg_*`); under strict identity a
high or critical store or update is refused with the typed `identity_required`
refusal and nothing is written. Pass your `client_session_id` to write as
yourself. Substrate-earned residents, which resolve by fingerprint by design,
keep their attribution, as they do for check-ins.

Under strict identity, naming yourself is not proof. A knowledge write that
passes an `agent_id` with no binding behind it (no `client_session_id`,
continuity token or other proof this call resolved) is refused with the same
typed `identity_required` refusal, whatever the severity, and nothing is
written. `supersede`, `cleanup`, `synthesize`, `promote` and `audit` have no
anonymous fallback: they refuse any call without a binding, with or without
an `agent_id`, and accept a server-inferred binding only for a substrate
resident. Leave `agent_id` out and pass the `client_session_id` from
`start_session`.

## Tagging Best Practices

Tags are how future agents find your contributions. Be intentional:

- **Include the domain**: `identity`, `database`, `performance`, `deployment`, `testing`
- **Include the type**: `bug`, `insight`, `pattern`, `config`, `dependency`
- **Include context**: `postgres`, `eisv`, `dialectic`, `discord-bridge`
- **Be specific**: `pool-connection-leak` is more useful than `bug`
- **Be consistent**: Check existing tags before inventing new ones
- **Keep them short**: a write with more than 50 tags, or a tag over 128
  characters (both counted after normalization), is refused; descriptive
  text belongs in `summary` or `details`
- **Mind the lifecycle tags**: `ephemeral`, `temp`, `scratch`, `test`, `demo`
  archive the entry after 7 days; `permanent`, `foundational`, `architecture`,
  `decision` (and the `architectural_decision` / `learning` / `pattern` types)
  make it permanent, and permanence wins on tie. A durable finding *about* the
  test suite must not carry the `test` tag.
- **Permanent means retained, not unchangeable**: it stops automatic archival.
  `store(..., supersedes=<older>)` refuses to supersede an entry only when it
  carries a permanent TAG, so an architectural decision can still be replaced
  by its next revision — which is that category's normal lifecycle.
  `knowledge(action="supersede")` does not check the tags.

## Closing the Loop

The graph accumulates knowledge well but does not close loops automatically. This is a known gap that every agent should help address:

- **When you resolve something, update its status and add
  `resolution_notes`.** Omitted fields are preserved; do not resend stale
  content just to close the row. When the new status is a closing one
  (`resolved`, `closed`, `wont_fix`, `superseded`), declare the standard you
  closed by with `closure_class`, a string: `fix_verified`, `unobserved`,
  `not_reproducible`, `obsolete` or `duplicate`. Its evidence goes in
  `closure_evidence`, an object: `deployed` and `observed` for
  `fix_verified`, `window` and `instrument_check` for `unobserved`. Keep each
  evidence value to a short statement or a pointer (a commit, a build_sha, a
  query): evidence over 8 KiB as stored JSON is refused, and long material
  such as a log excerpt belongs in `resolution_notes`. Notes are appended to
  the finding's details, and an update whose stored details would exceed 96
  KiB is refused, so point to a file, commit or run rather than pasting a
  whole log. Both are
  parameters of `update_finding` and `knowledge(action="update")`, are
  validated, and are stored. The response's `closure_class` is the value read
  back from the record. `knowledge(action="details")` returns the class and
  its evidence. Search results name the class in `full` and `compact` mode
  (the `knowledge` router defaults to `full`); `search_shared_memory` defaults
  to the `lean` digest, which carries `closure_class` when set and, on a
  closing row (`resolved`, `closed`, `wont_fix` or `superseded`) with no class,
  `closure_standard: "undeclared"`
  (a label, not a verdict: the row declared no standard). The class stays with the
  finding when the lifecycle archives it or moves it to cold, and an update
  that reopens it (`open` or `disputed`) clears both. Sent without a status, a
  class classifies a finding that is already closed, and is refused on an
  open one. A closure that declares no class is accepted and answered with
  `closure_class: null` and a `closure_class_note` naming the call that adds
  one. On another agent's high or critical finding, `resolution_notes` and
  `closure_class` are accepted only in the same call as a cross-agent closing
  status (`resolved`, `closed` or `wont_fix`).
- **When you find a duplicate, archive the less complete one** and reference the better entry
  (`closure_class="duplicate"` records why).
- **When a finding is outdated, archive it** with a note about what superseded it.
- **Periodically audit stale open entries** with `knowledge(action="audit")`
  (read-only), and run `knowledge(action="cleanup")` (dry-run by default) to
  apply the lifecycle archival passes. Cleanup has no domain or tag scope, and
  it does not score staleness — that is `audit`'s job. It does write to open
  rows: the tag-canonicalization pass rewrites their tags, and the ephemeral
  pass archives `ephemeral`-tagged open rows older than 7 days.

Unresolved entries create noise. Closed loops create trust in the graph.
Search flags an open entry whose latest write (`updated_at` when present, not
merely the original creation time) is more than 60 days old, so a genuinely
maintained finding does not age as if untouched. The result carries
`staleness_warning` (a sentence) and `last_activity_days` (days since that
write, in UTC; `knowledge(action="audit")` uses the same name for days since
its own last-activity time, computed in server-local time, so the two can
differ by a day near a day boundary; the audit's `age_days` counts from
creation). For a durable entry, one the
lifecycle keeps permanently (the permanent types and tags above) or an
`insight`, the sentence gives the age without calling the entry "still open",
since open is its resting status. It still says to verify,
because durable rules carry volatile details such as ports, paths and refs.
The classification is the retention one, so an open bug tagged `architecture`
also gets the neutral sentence. There is no release-version clause:
`system_version` on a result is store-time provenance, not a staleness signal.
The default `search_shared_memory` digest carries `last_activity_days` without
the sentence and explains the field once, in `state_summary.staleness_note`.

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

Generated rollup statements carry discovery IDs for their supporting sources.
The server checks that every reference belongs to the bounded source sample;
missing or invalid references cause a deterministic source-list fallback.
Use those IDs to inspect the evidence before relying on a generated claim:
reference validation establishes traceability, not factual entailment. The
sample is capped at 12 discoveries and each summary excerpt at 1,000 characters,
so a rollup does not establish that the whole topic has been covered.

Run it **on demand or on a periodic cadence (like cleanup/lint), not on every
write** — a per-write LLM pass across a multi-agent fleet is exactly the
high-frequency-noise anti-pattern this graph avoids.

The server also runs that cadence itself, set by
`UNITARES_KG_SYNTHESIS_SCHEDULE`. The default, `shadow`, previews rollups
with the deterministic narrative and only logs them, so nothing is written;
`apply` writes them as the `system:kg-synthesis` writer, with the local-LLM
narrative and its deterministic fallback; `off` disables it. When a store
finds three or more open near-duplicates, its response carries a
`consolidation_hint`, and the tags the new finding shares with those
duplicates (its own semantic tags when it shares none) are queued as topics
for the next pass. That queue is in memory and lost on restart. The hint still asks you to supersede or resolve the duplicates: a
rollup summarizes them and leaves each one open.

### Imported-memory previews

Search and agent-scoped get cap imported inline details at 2 KiB UTF-8, even
in full mode. A truncated row reports its original character/byte lengths and
`details_retrieval` arguments; use `knowledge(action="details", discovery_id=...,
offset=0, length=2000)` and the returned pagination to read the stored evidence.
Native findings and governed promotions keep their existing behavior.

### Appended notes on a retrieved record

A summary can predate later evidence. Search, agent-scoped get and details reads
carry `resolution_notes` when the full stored details contain recognizable dated
`Resolution notes (...)` blocks. It reports `source="details_text"`, `count`,
`first_offset` and `latest_offset`. These are character offsets, in the same units
as details pagination, computed before previews or imported-memory truncation.

Open the last block with `knowledge(action="details", discovery_id=...,
offset=<latest_offset>)`; start at `first_offset` to inspect the note history.
The last block may be administrative while an earlier one contains the correction.
Use the returned pagination to continue through long notes. On a later read,
compare its fresh note locations before reusing offsets: replacing details can
move or remove them.

These locations are parsed from authored text, not authenticated update receipts.
“Latest” means last in text order, not most authoritative. A note can confirm the
original claim, correct it, or append unrelated progress; neither `resolved` nor
the presence of notes means the summary is false. Read the evidence before acting.
Original content, authorship, lifecycle and ranking are unchanged.

The imported digest's `imported_memory` marker says `trust="unreviewed"` and
gives `age_days` since the KG row was created (`age_basis="created_at"`), or null
if unknown. This is not source age or proof of review; a routine sync does not
rejuvenate it. Markers share the existing digest budgets, so fewer rows may fit.

## Active-session change notices

Successful tool responses may include `knowledge_changes`, with up to three
coalesced row/revision pointers. Read the current details before acting; a notice
is not authority. Acknowledge only received revisions with
`knowledge(action="notices", acknowledge=["<revision UUID>"])`. Reading alone
replays the pointer. An old receipt cannot erase a newer revision.

The inbox belongs to the proved process UUID. Exact `notify-agent-<UUID>` tags
address a row; prose/display names and predecessor identities do not. Use
`knowledge(action="notices", muted=true)` to suppress automatic display, or
`muted=false` to restore it. The queue keeps at most 100 row pointers for seven
days. A `gap_token` means rescan relevant rows, then acknowledge that exact token
with `acknowledge_gap`. No wake, background model call, automatic check-in or KG
status change occurs. Writer identity is not guessed; these pointers omit actor
attribution. Migration 074 must be deployed before the capability is available.
