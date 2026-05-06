# S7 - KG Provenance Lineage Schema

**Status:** Schema decision v0.1 + persistence round-trip + authoritative snapshot builder shipped. No migration.
**Last Updated:** 2026-05-06
**Scope:** Plan row S7 (`docs/ontology/plan.md`). Defines how KG discoveries should represent writer identity, lineage state, and chain aggregation without treating UUID continuity or declared parentage as automatically confirmed.
**Builds on:** R1 provisional-lineage marking, R2 Phase 1 lineage lifecycle, R3 trust-tier annotation, R5 KG cite-and-extend, R6/S22 provenance envelope vocabulary.

---

## Purpose

S7 answers one narrow question: when a KG discovery is written, what provenance should be stored so future queries can distinguish:

- "this UUID authored the row",
- "this row was written by a successor that declared a parent",
- "the declared lineage was confirmed at write time",
- "the lineage is still confirmed now",
- "this row should count in lineage-attributed aggregation by default."

The current `agent_id` column is necessary, but not enough. It identifies the writer UUID. It does not say whether a write should be attributed to a lineage chain, whether the chain was provisional, or whether later demotion invalidated chain-level interpretation.

## Existing State

The storage schema already has the right rough slots:

- `knowledge.discoveries.agent_id`
- `knowledge.discoveries.provenance JSONB`
- `knowledge.discoveries.provenance_chain JSONB`

The runtime does not yet use them consistently for S7:

- `src/mcp_handlers/knowledge/handlers.py` computes a `provenance_chain` list from `agent_metadata`, and as of 2026-05-06 the single-store path passes it into `DiscoveryNode`.
- `src/db/mixins/knowledge_graph.py::kg_add_discovery` now inserts and conflict-updates `provenance_chain`.
- `src/storage/knowledge_graph_postgres.py::_dict_to_discovery` now hydrates `provenance_chain`.
- The current chain builder lives at `src/identity/provenance_chain.py`. It walks `core.identities` via `read_lineage_state`, emits S7 `lineage_link.v1` records with R2 lifecycle columns, orders links root-to-writer, and stops with explicit reasons for missing parents, cycles, or max-depth cutoffs. The KG handler falls back to metadata-derived ancestry only when the authoritative DB snapshot path errors.

So S7 does not need a new table first. It needs the existing JSONB column to carry an ontology-correct chain snapshot and a query policy for current-valid aggregation.

## Decision

Use `knowledge.discoveries.provenance_chain` as an immutable write-time lineage snapshot, and use current `core.identities` state for default aggregation validity.

Do not add a `lineage_edges` table in S7 v0.1. R2 intentionally keeps the canonical active edge on `core.identities.parent_agent_id` plus lifecycle columns. A separate edge table becomes justified only if one of these appears:

- multi-parent lineage,
- multiple active edges per successor,
- high-volume chain queries that cannot be served by JSONB plus current-state lookups,
- a need to preserve every edge-state transition outside audit events.

None of those are true yet.

## Field Contract

`agent_id` remains the writer UUID. It is not a lineage root and not a role identity.

`provenance` remains write-local context. Current fields such as `system_version`, `captured_at`, `source`, `writer_label_at_write`, and `writer_session_id_at_write` stay there. S22 can later add harness/model/transport fields under this same write-local envelope.

`provenance_chain` becomes a list of lineage-link snapshots, ordered root-to-writer. Each entry describes one parent -> successor link that was active or visible at write time:

```json
{
  "schema": "s7.lineage_link.v1",
  "source": "core.identities",
  "parent_agent_id": "uuid",
  "successor_agent_id": "uuid",
  "relationship": "lineage_parent",
  "lineage_state": "confirmed",
  "provisional_lineage": false,
  "lineage_declared_at": "2026-05-06T00:00:00+00:00",
  "confirmed_at": "2026-05-06T01:00:00+00:00",
  "lineage_demoted_at": null,
  "lineage_archived_at": null,
  "chain_obs_count": 3,
  "depth_from_writer": 1,
  "aggregation_eligible_at_write": true
}
```

Allowed `lineage_state` values:

| State | Meaning | Default aggregation |
|---|---|---|
| `confirmed` | Parent link was promoted by R1/R2 and not terminal at write time. | Eligible at write time. |
| `provisional` | Parent link was declared but not promoted. | Visible, excluded by default. |
| `archived` | Grace expired; retained for audit context only. | Excluded. |
| `demoted` | Link was rejected after evidence. | Excluded. |

`aggregation_eligible_at_write` is true only when this link is confirmed. A multi-hop chain is write-eligible only if every link in the list is eligible.

## Aggregation Semantics

S7 distinguishes two query modes:

| Mode | Meaning | Default user |
|---|---|---|
| `as_written` | Interpret the discovery using the chain state captured at write time. | Audit/debug views. |
| `current_valid` | Interpret the discovery only if the chain links are still confirmed in current `core.identities` state. | Dashboards, trust-tier-adjacent metrics, lineage-attributed KG counts. |

`current_valid` is the default for lineage-attributed aggregation. This is required by R2's confirmed -> demoted clawback rule: a write that was chain-eligible at write time must stop counting toward current chain-attributed metrics if a link is later demoted.

`as_written` remains available because historical interpretation is still useful. It answers "what did the system believe when this row was written?", not "what should count now?"

Provisional links are never included in default lineage-attributed aggregation. Operators may opt in with an explicit `include_provisional=true` style query flag, but that surface must label the result as provisional.

## Implementation Sequence

1. **PR 0: this schema decision.** Doc-only. No runtime behavior.
2. **PR 1: persistence round-trip.** Done 2026-05-06. `DiscoveryNode.provenance_chain` is passed by the single-store KG handler, inserted by the PostgreSQL backend, hydrated on read, and covered by unit tests. No schema migration; the column already exists.
3. **PR 2: authoritative snapshot builder.** Done 2026-05-06 in `src/identity/provenance_chain.py`. It walks `core.identities`, records R2 lifecycle columns per link, serializes timestamps, marks aggregation eligibility, and stops with `chain_stop_reason` instead of fabricating missing ancestry.
4. **PR 3: aggregation helper.** Next. Add a read helper that can answer `as_written` vs `current_valid`, excluding provisional links by default. This is the first place lineage-attributed KG counts should use the new contract.
5. **PR 4: S22 merge point.** Add harness/model/transport/tool-surface fields to `provenance` once S22 field names are settled. Keep lineage links in `provenance_chain`; do not mix harness facts into chain links.
6. **PR 5: indexing decision.** Add JSONB or generated-column indexes only after query volume shows they are needed.

## Non-Goals

- No R2 Phase 2 consumer flip in this decision.
- No trust-tier promotion changes.
- No new lineage table in v0.1.
- No retroactive rewrite of existing KG rows.
- No semantic proof that a lineage-linked write actually used parent memory. R5 owns that channel.
- No security claim. KG provenance is interpretive metadata, not authentication.

## Open Questions

1. Should the persisted `provenance_chain` include only active ancestors, or should it include terminal links visible in audit history? v0.1 says active/current chain only; terminal state is handled by current-valid queries and audit events.
2. Should `current_valid` aggregation read live `core.identities` on every query, or materialize a compact validity cache? v0.1 says read live until volume proves otherwise.
3. Should multi-generation chain aggregation require every link confirmed, or allow partial confirmed prefixes? v0.1 says every link for default aggregation; partial prefixes require explicit query selection.
4. Should R5 include ancestor parent memory once S7 lands? R5 v0.1 says no; revisit only after S7 PR 2 produces reliable chain snapshots.
