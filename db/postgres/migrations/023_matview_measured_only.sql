-- 023_matview_measured_only.sql  (renumbered from 019)
--
-- Phase 3a of onboard-bootstrap-checkin.md (v2.1).
--
-- Bakes `WHERE synthetic = false` into the core.mv_latest_agent_states
-- definition itself so the matview rowset never contains bootstrap rows.
-- Migration 022 already creates this measured-only definition in the
-- renumbered drift-repair path; this migration preserves the original phase
-- boundary and registry marker while confirming the same terminal shape.
--
-- Why: a reader who never expects synthetic rows in the matview can't
-- accidentally introduce a bug by writing a SELECT that omits the filter.
-- The base-table fallback in get_all_latest_agent_states still needs an
-- explicit WHERE clause because it queries the base table directly.
--
-- Rollback shape: DROP MATERIALIZED VIEW + recreate without the WHERE
-- clause (i.e. revert to migration 022's projection).

-- Drop the prior matview and recreate the same measured-only terminal shape.
DROP MATERIALIZED VIEW IF EXISTS core.mv_latest_agent_states;

-- Measured-only: DISTINCT ON identity, latest measured row only. Bootstrap
-- rows are excluded at the matview-definition level.
CREATE MATERIALIZED VIEW core.mv_latest_agent_states AS
SELECT DISTINCT ON (s.identity_id)
       s.state_id, s.identity_id, i.agent_id, s.recorded_at,
       s.entropy, s.integrity, s.stability_index, s.volatility,
       s.regime, s.coherence, s.state_json, s.synthetic
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
WHERE s.synthetic = false
ORDER BY s.identity_id, s.recorded_at DESC;

-- Unique index required for REFRESH CONCURRENTLY (mirrors migration 008).
CREATE UNIQUE INDEX idx_mv_latest_states_identity
    ON core.mv_latest_agent_states (identity_id);

-- For lookups by agent_id (mirrors migration 008).
CREATE INDEX idx_mv_latest_states_agent
    ON core.mv_latest_agent_states (agent_id);

-- Register the migration.
-- NOTE: renumbered 019→023 to enforce correct apply order after 022.
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (23, 'matview_measured_only', NOW())
ON CONFLICT (version) DO NOTHING;
