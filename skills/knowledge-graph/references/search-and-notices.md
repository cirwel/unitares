# Search Ranking, Closure Caps, Appended Notes, and Change Notices

Reference companion to `skills/knowledge-graph/SKILL.md`. Load when handling
`more_results`, `archived_cold_fallback`, `imported_omitted`, `closure_class`
edge cases, `staleness_warning`, `resolution_notes` offsets, or
`knowledge_changes` / `gap_token`.

## Search Tiers, Filtering, and Newest-First Controls

- **Tag and author filters**: tags are exact any-of filters, not a complete
  inventory — if your own narrowing tags miss, retry without them while keeping
  explicit user/task filters intact (never silently widen a filtered request or
  retag records to rescue an evaluation). You may omit `query` when filtering by
  `tags`, `discovery_type`, `severity`, `status`, or `agent_id_filter` (which
  takes precedence over legacy `agent_id`). Blank explicit filters and
  supplied-but-blank `query` strings are rejected; whitespace is trimmed.
- **Digest tiers (`lean`, `compact`, `full`)**: `lean` and `compact` return 3
  digests regardless of `limit`, plus up to 17 summary rows under `more_results`
  (`discovery_id`, `type`, `status`, short summary; counted in
  `state_summary.more_results_shown`). Pass `limit <= 3` for the 3 digests
  alone. When digests carry scores on multiple `relevance_basis` values,
  `state_summary.relevance_note` notes they are not comparable — list order is
  the ranking. Omit `include_details` on `knowledge(action="search")` to let
  the server auto-expand up to 3 hits; `search_shared_memory` forces
  `include_details=false` in `lean` mode (disclosing if `include_details=true`
  was overridden). `lean` keeps result tier and `open_one` route; `compact`
  keeps diagnostics; `full` returns the canonical payload. Pass
  `include_provenance=true` when provenance fields are needed.
- **Newest-first and recency**:
  - `sort_by="created_at"` orders FTS matches newest first (incompatible with
    `search_mode="semantic"`/`"hybrid"` or `semantic=true`). Multi-term FTS
    queries default to AND and fall back to OR only when AND yields no visible
    candidate (including when every AND match is filtered out by
    `exclude_agent_labels`); fallback is skipped when `operator` is explicitly
    forced, when `sort_by="created_at"` returns a full first page (so older AND
    pages can still match), or when a query exceeds 24 terms (which sets
    `fts_fallback_skipped_reason` in `full` responses, or `compact` on
    `knowledge(action="search")`; pass `operator="OR"` for broad recall).
  - `created_after` / `created_before` take exclusive ISO 8601 timestamps (UTC
    if no offset; `"2026-09-26"` = midnight UTC) and work with any order or
    search mode. Queryless `created_after` lists what is new since `T`, newest
    first.
  - `recency_half_life_days` keeps relevance order while multiplying scores by
    `0.5 ** (age_days / N)` (requires a query; cannot combine with
    `sort_by="created_at"`).
  - Unparseable or inverted time windows are refused, not ignored. The response
    echoes non-default `sort_by`, window, and `recency_half_life_days`.
- **Archived and cold fallback**: a query search (no `status` filter) that finds
  fewer than 3 non-archived/non-cold rows runs an optional second pass including
  `archived` and `cold` rows, appended last with
  `retrieval_tier="archived_cold_fallback"` and halved scores (counts in
  `archived_cold_fallback` / `state_summary.archived_cold_fallback`). If that
  pass fails or runs out of search time budget, `archived_cold_fallback_error`
  (lean: `state_summary.archived_cold_fallback_error`) explains why; pass
  `include_archived=true` / `include_cold=true` directly to query them, or
  `include_archived=false` / `include_cold=false` to exclude them explicitly.
  Queryless listings and `status`-filtered searches never run the fallback pass.
- **`list` and `audit` parameters**: `knowledge(action="list")` returns raw
  status aggregates (`epoch_scope`, `including_cold`), intentionally differing
  from `stats` lifecycle buckets. `knowledge(action="audit")` accepts
  `scope` (`open` | `all` | `by_agent`) and `top_n` (default 10).
- **Superseded links on search**: on the AGE backend, `supersede` creates a
  `SUPERSEDES` edge so search digests and `details` carry `superseded_by` (the
  first successor ID). On the default Postgres backend, `update(status="superseded", superseded_by=...)`
  or `store(..., supersedes=...)` emits a `supersession_warning` that no graph
  edge was recorded. A search that hits its time budget may flag
  `superseded: true` without `superseded_by` — read the row directly.

## Closure Contract, Limits, and Staleness Warnings

- **Size limits and validation**: `summary` over 4,003 characters is refused;
  `closure_evidence` over 8 KiB stored JSON is refused; `update` pushing stored
  `details` (including appended `resolution_notes`) over 96 KiB is refused.
- **`closure_class` readback and rules**:
  - Closing without `closure_class` is accepted with `closure_class: null` and
    a `closure_class_note` naming the call to add one; `search_shared_memory`
    lean digests mark closed rows without a class as
    `closure_standard: "undeclared"` (a label, not a verdict).
  - Sending `closure_class` without a status classifies an already-closed row
    and is refused on an open row. The class survives lifecycle archival to
    `archived` / `cold`.
  - On another agent's high or critical finding, `resolution_notes` and
    `closure_class` are accepted only in the same call as a cross-agent closing
    status (`resolved`, `closed`, or `wont_fix`).
- **Staleness warnings**: search flags an open entry whose latest write
  (`updated_at` when present) is more than 60 days old with `staleness_warning`
  and `last_activity_days` (UTC; `knowledge(action="audit")` uses server-local
  time for `last_activity_days` and creation time for `age_days`, so they can
  differ by a day near midnight). Durable/permanent entries and `insight` rows
  receive a neutral verification reminder rather than "still open."
  `system_version` is store-time provenance, not a staleness signal. Lean
  digests carry `last_activity_days` and explain it once in
  `state_summary.staleness_note`.
- **Cleanup scope**: `knowledge(action="cleanup")` has no domain or tag scope
  and does not score staleness (`audit` does). With `dry_run=false`, it
  canonicalizes tags on open rows, archives `ephemeral`-tagged rows after 7
  days, archives resolved non-permanent rows after 30 days, and moves archived
  rows to `cold` after 90 days; it never deletes.

## Imported-Memory Previews and Appended Note Offsets

- **Imported-memory previews**: search and agent-scoped `get` cap inline
  imported `details` at 2 KiB UTF-8 even in `full` mode, returning original
  lengths and `details_retrieval` pagination arguments
  (`knowledge(action="details", discovery_id=..., offset=0, length=2000)`).
  Imported digests carry `imported_memory` with `trust="unreviewed"` and
  `age_days` (`age_basis="created_at"`, not source age or review proof; routine
  sync does not rejuvenate it).
- **Appended `resolution_notes` offsets**: search, agent-scoped `get`, and
  `details` report `resolution_notes` (`source="details_text"`, `count`,
  `first_offset`, `latest_offset` in character offsets computed before previews
  or truncation) when stored `details` contain dated `Resolution notes (...)`
  blocks. Open the latest block with `offset=<latest_offset>` or history from
  `first_offset`. Locations are parsed from authored text, not authenticated
  receipts: "latest" is text order, not authority, and replacing `details` moves
  offsets.
- **Response chains**: `knowledge(action="details", include_response_chain=true)`
  returns the typed response chain on AGE, bounded by `max_chain_depth`
  (default 10, max 32), `response_chain_limit` (default 100, max 200), and
  `response_chain_offset` (default 0). `store_finding` confirms a linked reply
  in `state_summary.response_to`.

## Active-Session Change Notices (`knowledge_changes`)

Successful tool responses may include `knowledge_changes` with up to three
coalesced row/revision pointers. Read the current row details before acting; a
notice is not authority.

- **Addressing**: the inbox belongs strictly to the proved process UUID. Exact
  `notify-agent-<UUID>` tags address a row; display names and predecessor
  identities do not.
- **Acknowledging**: acknowledge only actually processed revision IDs via
  `knowledge(action="notices", acknowledge=["<revision UUID>"])` so they stop
  repeating. Never acknowledge unseen revisions or mute just to reduce context.
  Reading alone replays the pointer, and an old receipt cannot erase a newer
  revision.
- **Muting and gaps**: `knowledge(action="notices", muted=true|false)` controls
  automatic display. The queue retains up to 100 row pointers for 7 days. A
  `gap_token` means rescan relevant rows, then pass that exact token to
  `acknowledge_gap`.
- **Non-side-effecting**: notice delivery triggers no wake, background model
  call, automatic check-in, or KG status change, and omits writer attribution.
  Requires migration 074.
