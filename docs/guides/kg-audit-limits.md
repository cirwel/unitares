# Reading a bounded knowledge-graph audit

**Last verified:** 2026-11-26

`knowledge(action="audit", scope="open", top_n=100, use_model=false)` scores
open discoveries and returns a bounded list for human or agent triage. It does
not archive anything. `candidate_for_archive` means old with no recorded
activity; verify the claim against its files, PRs or successor before closing it.
Insights and unresolved questions can legitimately remain open.

There are three distinct bounds:

- `run_kg_audit` sorts stale and candidate entries by last activity and takes
  the first `max(0, top_n)` (default 10; non-positive `top_n` returns `[]`).
  Its source query fetches at most 10,000 open rows; `scope="all"` fetches up
  to 5,000 each of open, resolved and archived. Here `all` does not include
  every possible status, such as cold or superseded.
- Each `top_stale` entry bounds `summary` to **240 characters**
  (`AUDIT_SUMMARY_MAX_CHARS`). Short summaries stay verbatim; entries over 240
  characters are cut at the last whitespace boundary in the second half of the
  budget (or hard-cut at 239 characters if none exists), end with `…`, and
  carry `summary_truncated: true` (open the row with
  `knowledge(action="details", discovery_id=...)` to read the full summary).
  When `use_model=true`, the optional local-LLM prompt inspects at most the
  first 100 `top_stale` entries (`AUDIT_MODEL_MAX_ENTRIES`).
- The shared MCP response serializer keeps the first **100 list entries** and
  appends a string such as `"... (6 more items)"` when more were selected.
  This also applies to `audit.top_stale`. `top_n=120` cannot deliver 120 row
  objects through this response path. The resulting list can have length 101
  because its last element is a marker, not a discovery.

The 2026-10-13 08:46 UTC read requested 120, selected 106 stale/candidate
entries, and returned 100 objects plus `"... (6 more items)"`. Its bucket
counts still described all 520 rows fetched for the audit. The marker counts
entries omitted from the **selected list**, not all omitted graph rows.
Do not use `len(top_stale)` as either the corpus size or the candidate count.

Request at most 100 for an all-object list and use `audit.buckets` and
`audit.total_audited` for the scan's counts. The audit currently has no offset
or cursor; increasing `top_n`, using full response mode or passing a details
pagination offset does not page this list. For a known writer, `scope="by_agent"`
with `agent_id` narrows the audit to that writer's open rows (after the bounded
open-row fetch). It is not a guarantee of complete corpus traversal.
Open any returned row with `knowledge(action="details", discovery_id=...)`;
that action has its own details pagination. A complete larger audit needs a
separately reviewed pagination design, not a global serializer-limit increase.

Implementation: `run_kg_audit` in
[`src/knowledge_graph_lifecycle.py`](../../src/knowledge_graph_lifecycle.py),
`handle_audit_knowledge_graph` in
[`src/mcp_handlers/knowledge/handlers.py`](../../src/mcp_handlers/knowledge/handlers.py),
and `_make_json_serializable` in
[`src/mcp_handlers/serialization.py`](../../src/mcp_handlers/serialization.py).

Source audit: discovery `2026-10-13T06:48:37.234783+00:00`.
