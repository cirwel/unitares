# Imported-memory lifecycle and bounded retrieval

**Status:** Friction fix authorized by the 2026-10-13 handoff addendum and KG
freeze-scope decision `2026-10-13T09:17:42.808916+00:00`. Preview and digest
implementation is in this PR; deployment remains separate. Source lifecycle
already replaces changed bodies and archives deleted/retracted sources.
**Evidence checked:** 2026-10-13.

## Problem and corrected baseline

The October 13 KG usability audit (discovery
`2026-10-13T06:48:37.234783+00:00`) found more than 100 open imported
Claude-memory rows and one roughly 65 KB body in a full search response.
These snapshots can outlive their useful context and overwhelm a reader.

The writer is outside this server repository. The operator's inspected copy is
`~/scripts/memory-tools/sync_kg.py`; the installed governance plugin contains
retrieval consumers such as `scripts/kg_recall.py`, not that sync implementation.
Check the owning maintenance project before proposing an implementation there.
No private memory files were needed for this source inspection.

Two corrections to the initial audit matter:

- The writer already caps `details` at **65,536 characters**, with a truncation
  notice (`MAX_DETAILS_CHARS`, `details_for_kg`). That is not a small inline
  response budget, and characters are not UTF-8 bytes.
- `plan` already updates changed content in place, skips unchanged representations,
  and archives deleted or policy-retracted sources. It retains an archived ID for
  explicit revival and stops writing a source with a known graph successor.
  It does not assign an expiry to an unchanged, still-shareable snapshot or retain
  each changed source as a separately linked historical revision.

The server already marks imported authority as `imported_context` / `unreviewed`
in the full search result (`src/knowledge_authority.py` and knowledge handlers).
The default digest and its age presentation need separate assessment. The
existing authority marker is not evidence that a digest reader saw it.
The October 10 memory-sync inversion delegation is recorded in discovery
`2026-10-10T10:55:59.352545+00:00`; this proposal must fit that direction rather
than restore harness memory as the source of truth. The operator's addendum explicitly authorizes the friction fixes during the freeze.

## Implemented scope and remaining design options

1. Bound imported inline details to a **2 KiB UTF-8 preview**, with the
   discovery ID, total size and explicit paginated-details retrieval. Keep the
   full stored evidence retrievable; a local filename alone is not a useful
   pointer for a remote agent. Apply the budget before response serialization.
2. Have the **sync owner** account for source revisions and expiry. Prefer a
   linked replacement for materially changed claims; keep deletion/retraction
   archival and idempotent reappearance handling. An unchanged source past an
   operator-selected review interval should be flagged for review before any
   automatic archival policy is enabled. Source-file mtime alone is not proof
   that the claim was reviewed.
3. Put `imported_memory` (unreviewed, age_days, age_basis=created_at) on imported
   digest rows within existing budgets. This is KG creation age, not source
   revision time or a verification claim. Sync/status updates do not reset it.

Do not add a blanket `ephemeral` tag to every import. Permanent types/tags win
over ephemeral retention, and an imported durable fact may be valid long term.
Do not manually archive sync-owned rows: that can race update/revival and
separate the mirror's local index from graph state.

## Acceptance evidence and decision

A later implementation should test multibyte budgets, empty/oversized bodies,
remote retrieval, source change/deletion/reappearance, interrupted sync,
idempotence, successor protection, and promoted claims remaining authoritative.
Measure returned bytes and whether a reader can reach the full evidence.
Interface/output changes require a dated contract bump, generated contract,
changelog and re-verified skill attestations; writer changes need their own
repository's review and delivery. No new model dependency is needed.

No new writer is added to the plugin: the actual writer is in `cirwel/fleet-ops`,
`memory-tools/sync_kg.py`. Existing in-place replacement retires the old body;
source deletion/retraction archives the row. Its 23 existing tests pass. An
arbitrary age-only TTL would discard unchanged durable content and is not
needed to make these existing paths work. Separate linked revision history
and a time-based expiry policy remain design options, not implemented changes.
