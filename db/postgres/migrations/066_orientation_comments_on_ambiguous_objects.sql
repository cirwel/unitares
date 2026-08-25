-- 066_orientation_comments_on_ambiguous_objects.sql
--
-- Documentation only. No DDL, no data change, no behavior change.
--
-- == Why this exists ==
--
-- Every object commented below returned a CLEAN, CONFIDENT, WRONG answer to a
-- reasonable question during a single session on 2026-08-24. None of them
-- errored. None looked suspicious. Each one answers a question adjacent to the
-- one that was actually asked, and nothing at the point of use says so.
--
-- The failures were not reasoning failures. In each case the correct
-- information already existed somewhere -- in a DDL comment in schema.sql, in a
-- module docstring, in a file header -- and was not visible to anyone reading
-- the object itself. `raw_hash` is the clearest case: schema.sql:380 has said
-- "Deduplication hash" since it was introduced, but `\d audit.events` shows a
-- column of that name with no annotation, and it was read as attestation.
--
-- This migration moves those warnings ONTO the objects. `\d+` and
-- pg_description now carry them, so they survive a stale checkout, they cannot
-- drift from the schema, and they reach any operator or agent inspecting the
-- database rather than only those who know which file to open.
--
-- This is the federating form of that knowledge. A peer instance built from
-- these migrations inherits the warnings; it would not inherit a markdown file
-- or a generated figure.
--
-- Extends existing practice -- see 034, 045, 033, 058, 065, and especially
-- audit.coordination_events.context ("Facts about the emitter ... NOT facts
-- about the event"), which is the model this follows.
--
-- == On core.agents ==
--
-- An earlier draft of this migration called core.agents a "thin legacy table."
-- That is FALSE and was caught before commit. It holds 6,610 rows with records
-- created the same day, and is read by dialectic_db.py, runtime_observations.py,
-- agent_metadata_persistence.py and others. agent_storage.py:81 states the
-- actual relationship: "Unified agent record combining data from core.agents
-- and core.identities." They are two halves of one record. The trap is not
-- staleness, it is the split -- model/client type and lineage live only in the
-- identities half, so a model-type question asked of core.agents finds nothing
-- and reads as absence rather than as wrong-half.
--
-- Counts are deliberately omitted from every comment below. A count dates; the
-- structural claim does not.

BEGIN;

COMMENT ON TABLE core.agents IS
'One half of the agent record: descriptive fields (purpose, tags, notes, label, status). The other half is core.identities (identity, lineage, metadata). agent_storage.py joins them into a unified AgentRecord -- neither table alone is "the agent". This half does NOT carry model/client type or lineage; asking it those questions returns absence, not an answer. For fleet population or model/client questions use core.identities.';

COMMENT ON TABLE core.identities IS
'The identity/lineage half of the agent record, and the registry anchor: core.agent_state.identity_id references it. The model/client marker is metadata->>''model_type'' (e.g. codex, gpt-5, claude-opus-5, dialectic_reviewer:codex) -- it exists ONLY here, not in core.agents. Fleet-population and "which agents/models are enrolled" questions resolve against this table.';

COMMENT ON COLUMN core.identities.metadata IS
'Free-form identity metadata. Load-bearing keys: model_type (model/client marker -- the only place it lives), label, source, structured_id, tags, purpose. Query model/client questions as metadata->>''model_type''; there is no dedicated column.';

COMMENT ON TABLE audit.events IS
'QUERYABLE INDEX over governance events, not the sole record. For events emitted through the legacy AuditLogger path (src/audit_log.py, 19 log_* methods incl. log_auto_attest), audit_log.py declares JSONL the durable truth: it writes an fsync''d, file-locked JSONL entry first, then a FIRE-AND-FORGET Postgres write whose failure is caught and logged, not raised. Its own docstring states "DB write loss is accepted". Other writers (the shared append_audit_event_async helper, and the Elixir lease-plane via raw Postgrex) write here ONLY, with no JSONL counterpart. So this table is a lossy secondary index for some event types and the sole record for others. Any completeness or evidence-sufficiency claim must name which writer it covers.';

COMMENT ON COLUMN audit.events.raw_hash IS
'DEDUPLICATION KEY, NOT ATTESTATION. Introduced to dedupe against the since-retired SQLite sync (see schema.sql). Populated only on the runtime_observation.* ingest path (src/runtime_observations.py canonicalizes with sort_keys + tight separators, SHA-256s, and persists row and hash together, forward-only). It is NULL on every other event type INCLUDING auto_attest, and that is correct, not a gap. It does not bind a record to its author: it sits in the same row, same table, same write authority as the payload it hashes, so any writer that can alter payload can alter it. Do not read a populated value as provenance, and do not backfill it -- jsonb does not preserve key order or whitespace, so a retroactive hash hashes Postgres''s own re-rendering and proves nothing about origin.';

COMMENT ON COLUMN audit.events.event_type IS
'Dotted or underscored family name. Values ending in _shadow (coherence_gate_shadow, grounding_shadow, basin_shadow) are EVALUATIONS THAT NEVER ACTUATE -- a gate computing what it would have done while a signal is under repair. Shadow volume runs orders of magnitude above actuation volume, so a rate computed with a shadow denominator reads as "operationally inert" while the system is actively intervening. Actuation lives in circuit_breaker_trip and lifecycle_paused here, and in core.dialectic_sessions (paused_agent_id IS NOT NULL) for review pauses. These are four distinct populations with four different counts; name the stream before quoting a pause rate.';

COMMIT;
