-- Migration 008: Materialized view for dashboard
--
-- The dashboard's get_all_latest_agent_states() does DISTINCT ON over the full
-- agent_state table, which gets slower as the table grows. This materialized
-- view pre-computes the latest state per identity and is refreshed after each
-- state insert (CONCURRENTLY, so reads are never blocked).

-- Drop if re-running migration
DROP MATERIALIZED VIEW IF EXISTS core.mv_latest_agent_states;

-- Create materialized view with all columns the dashboard needs
CREATE MATERIALIZED VIEW core.mv_latest_agent_states AS
SELECT DISTINCT ON (s.identity_id)
       s.state_id, s.identity_id, i.agent_id, s.recorded_at,
       s.entropy, s.integrity, s.stability_index, s.volatility,
       s.regime, s.coherence, s.state_json
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
ORDER BY s.identity_id, s.recorded_at DESC;

-- Unique index required for REFRESH CONCURRENTLY
CREATE UNIQUE INDEX idx_mv_latest_states_identity
    ON core.mv_latest_agent_states (identity_id);

-- For lookups by agent_id
CREATE INDEX idx_mv_latest_states_agent
    ON core.mv_latest_agent_states (agent_id);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (8, 'dashboard_matview', NOW())
ON CONFLICT (version) DO NOTHING;
