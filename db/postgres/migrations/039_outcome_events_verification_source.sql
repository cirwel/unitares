-- Migration 039: promote audit.outcome_events.verification_source from detail JSONB
-- to a top-level column.
--
-- Slot note: 038 was taken locally (2026-05-19 18:01) by in-flight Sentinel /
-- BEAM work (`agent_state_risk_score`); claiming 039 to avoid the collision
-- pattern documented in CLAUDE.md Single-Writer Surface guidance.
--
-- Background: src/mcp_handlers/schemas/core.py:280-291 already defines
-- verification_source on OutcomeEventParams as a 3-value Literal
-- (agent_reported_tool_result | server_observation | external_signal) with
-- agent_reported_tool_result as default. The MCP-tool path writes it into
-- `detail` JSONB at outcome_events.py:279. The direct db.record_outcome_event
-- callers (CIRS resonance, task_completed/failed, trajectory_validated in
-- src/mcp_handlers/updates/phases.py) bypass that code path entirely and
-- emit rows with no verification_source key at all — making the field
-- unusable for downstream queries.
--
-- Phase 1 of the outcome-anchored basin replan (council-revised 2026-05-19
-- after the 99.8%-NULL data finding) promotes the field to a top-level
-- column so all 7 emitters write it consistently, and downstream analytic
-- queries can filter / group by provenance without parsing JSONB.
--
-- Scope (this migration):
--   1. Add nullable verification_source TEXT column.
--   2. Backfill from detail->>'verification_source' where present (~105 rows).
--   3. CHECK constraint pinning the 3-value v1 enum.
--   4. Leave the JSONB key alone — code in this PR writes to the column
--      only (new path), JSONB rows from before backfill are left untouched
--      for forensic audit. A future migration may drop the JSONB key after
--      burn-in confirms column / JSONB agreement on new writes.
--
-- Out of scope:
--   - NOT NULL constraint (deferred until all 7 emitter sites are
--     measured to populate the field in production; flag-gated rollout
--     per STRICT_IDENTITY_REQUIRED precedent in CLAUDE.md).
--   - Taxonomy redesign (architect council surfaced source/claimant/
--     strength axis concerns 2026-05-19 — deferred to v2 schema motion;
--     see project_outcome-verification-taxonomy-redesign in memory).
--
-- Partition propagation: audit.outcome_events is RANGE-partitioned on ts
-- (5 monthly partitions as of 2026-05-19). PostgreSQL 17 propagates ADD
-- COLUMN to all child partitions automatically; no per-partition DDL.

ALTER TABLE audit.outcome_events
    ADD COLUMN IF NOT EXISTS verification_source TEXT;

COMMENT ON COLUMN audit.outcome_events.verification_source IS
    'Provenance of this outcome event. v1 enum: '
    'agent_reported_tool_result | server_observation | external_signal. '
    'Schema mirror of OutcomeEventParams.verification_source in '
    'src/mcp_handlers/schemas/core.py. NULL only on historical rows '
    'predating Phase 1 promotion (migration 038, 2026-05-19).';

-- Backfill from detail JSONB. The MCP-tool path and Phase-5 evidence loop
-- have been writing this key since the schema landed; ~105 rows carry it.
UPDATE audit.outcome_events
    SET verification_source = detail->>'verification_source'
    WHERE verification_source IS NULL
      AND detail ? 'verification_source';

-- v1 enum guard. Keeps NULL valid so historical rows don't break the
-- constraint; new writes (which all pass a value) get the enum check.
-- Wrapped in DO block to make the migration idempotent across reruns
-- (the constraint has no IF NOT EXISTS form in PG17).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'outcome_events_verification_source_v1_check'
    ) THEN
        ALTER TABLE audit.outcome_events
            ADD CONSTRAINT outcome_events_verification_source_v1_check
            CHECK (
                verification_source IS NULL
                OR verification_source IN (
                    'agent_reported_tool_result',
                    'server_observation',
                    'external_signal'
                )
            );
    END IF;
END$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (39, 'outcome_events_verification_source', NOW())
ON CONFLICT (version) DO NOTHING;
