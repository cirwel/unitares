-- 040_agent_state_epistemic_class.sql
--
-- Forward-only warrant label for core.agent_state rows. This intentionally
-- does NOT backfill historical rows: pre-040 process_agent_update inputs
-- (response_text / complexity / confidence) were not persisted in a
-- recoverable form, so old rows must remain epistemically opaque rather than
-- be relabeled by regex or transport heuristics.
--
-- `epistemic_class` avoids the existing sequential_calibration.signal_source
-- vocabulary, which routes hard-outcome channels such as "tests" and "tasks".
--
-- Values:
--   agent_report              agent-authored process_agent_update report
--   substrate_observation     raw substrate fact / measurement
--   substrate_interpretation  hook/tool-derived interpretation or heuristic
--   prediction                explicit forward claim intended for scoring
--   synthetic                 server-authored bootstrap/synthetic row
--
-- This migration does not drop/recreate core.mv_latest_agent_states. Existing
-- deployments keep the old materialized-view projection; the Python read path
-- falls back to the base table when the matview lacks epistemic_class. Fresh
-- installs from schema.sql expose the column in the matview immediately.
--
-- Rollback shape: remove column epistemic_class and its index/constraint in a
-- separately approved rollback migration.

ALTER TABLE core.agent_state
    ADD COLUMN IF NOT EXISTS epistemic_class text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_agent_state_epistemic_class'
          AND conrelid = 'core.agent_state'::regclass
    ) THEN
        ALTER TABLE core.agent_state
            ADD CONSTRAINT ck_agent_state_epistemic_class
            CHECK (
                epistemic_class IS NULL OR epistemic_class IN (
                    'agent_report',
                    'substrate_observation',
                    'substrate_interpretation',
                    'prediction',
                    'synthetic'
                )
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agent_state_epistemic_class
    ON core.agent_state (epistemic_class, recorded_at DESC)
    WHERE epistemic_class IS NOT NULL;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (40, 'agent_state_epistemic_class', NOW())
ON CONFLICT (version) DO NOTHING;
