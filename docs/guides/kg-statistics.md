# Reading knowledge-graph statistics

**Last verified:** 2026-10-13

`knowledge(action="stats")` reports two populations. Use
`stats.canonical_current_total` for the corpus total, and read its scope from
`stats.raw_current_counts.scope`. Use `stats.lifecycle_managed_counts` for the
four statuses the lifecycle manager counts. The legacy top-level
`stats.total_discoveries` is that lifecycle subtotal, not a second corpus total.

| Field | Population |
|---|---|
| `canonical_current_total` | All statuses in the backend's reported scope |
| `raw_current_counts.by_status` | The breakdown supporting that corpus total |
| `lifecycle_managed_counts.total_discoveries` | Open, resolved, archived and cold rows |
| `stats.total_discoveries` / `stats.by_status` | Legacy names for the lifecycle counts |
| `statuses_outside_lifecycle_buckets` | Nonzero raw statuses absent from the lifecycle buckets, when totals differ |

The 2026-10-13 usability audit's **2,005 versus 2,057** was explained by
**40 superseded + 12 closed = 52** rows outside the lifecycle buckets. A later
read at 08:45 UTC returned **2,008 + 41 superseded + 12 closed = 2,061**.
These are dated observations, not expected current values. No rows disappeared.
The live response already includes `count_scope_warning` explaining this
distinction; the labeling shipped in [PR #1873](https://github.com/cirwel/unitares/pull/1873).

Do not assume every difference has the same cause. Lifecycle queries span all
epochs; the raw aggregate requests the current epoch, but AGE has no epoch
property and reports all epochs. Compare the two scope objects before doing
arithmetic. Lifecycle queries also have a 10,000-row limit per status and the
reads are not one transactional snapshot. The raw aggregate can therefore
remain the corpus-size authority even when subtracting the outside statuses
does not reconcile the legacy subtotal. If raw aggregation fails, the response
reports `raw_current_counts_error`; do not promote the subtotal to a canonical
count or treat missing counts as zero.

`lifecycle_candidates` and `knowledge(action="audit")` answer different
questions. Lifecycle candidates follow retention rules (including permanent
entry exemptions); audit buckets use age and activity as triage signals.
`candidate_for_archive` is not proof that a claim is obsolete and does not
authorize bulk archival. Open insights can remain useful indefinitely.

Implementation: `KnowledgeGraphLifecycle.get_lifecycle_stats` in
[`src/knowledge_graph_lifecycle.py`](../../src/knowledge_graph_lifecycle.py),
`handle_get_lifecycle_stats` in
[`src/mcp_handlers/knowledge/handlers.py`](../../src/mcp_handlers/knowledge/handlers.py),
and the backend's `get_stats` in
[`src/storage/knowledge_graph_age.py`](../../src/storage/knowledge_graph_age.py)
or [`src/storage/knowledge_graph_postgres.py`](../../src/storage/knowledge_graph_postgres.py).

Source audit: discovery `2026-10-13T06:48:37.234783+00:00` (read with
`knowledge(action="details", discovery_id=...)`).
