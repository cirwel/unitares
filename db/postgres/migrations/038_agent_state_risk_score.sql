-- 038_agent_state_risk_score.sql
--
-- Promote `risk_score` from state_json (already written by agent_storage.record_agent_state)
-- to a typed column on core.agent_state, so fleet aggregation can compute mean_risk
-- in SQL without reading every monitor's in-memory state.
--
-- Context: observe.aggregate currently iterates mcp_server.monitors and means
-- monitor.state.risk_score. The in-memory monitors are load-once-per-process and
-- drift from Postgres truth on every cross-process write. The fix is to query
-- core.mv_latest_agent_states directly; that matview needs risk_score to do the job.
--
-- Backfill source: state_json->>'risk_score' is populated by record_agent_state
-- at src/agent_storage.py:572. Backfill is best-effort (NULL where state_json
-- never carried the field — pre-grounding rows, bootstrap rows).
--
-- Rollback shape: ALTER TABLE DROP COLUMN risk_score; recreate matview without
-- the column.

ALTER TABLE core.agent_state
    ADD COLUMN IF NOT EXISTS risk_score real;

-- Backfill from existing state_json. Best-effort cast — invalid values stay NULL.
UPDATE core.agent_state
SET risk_score = (state_json ->> 'risk_score')::real
WHERE risk_score IS NULL
  AND state_json ? 'risk_score'
  AND (state_json ->> 'risk_score') ~ '^-?[0-9]+(\.[0-9]+)?$';

-- Drop+recreate matview to include risk_score. Same shape as migration 023,
-- plus the new column. Cascade not needed — no dependent views.
DROP MATERIALIZED VIEW IF EXISTS core.mv_latest_agent_states;

CREATE MATERIALIZED VIEW core.mv_latest_agent_states AS
SELECT DISTINCT ON (s.identity_id)
       s.state_id, s.identity_id, i.agent_id, s.recorded_at,
       s.entropy, s.integrity, s.stability_index, s.volatility,
       s.regime, s.coherence, s.risk_score, s.state_json, s.synthetic
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
WHERE s.synthetic = false
ORDER BY s.identity_id, s.recorded_at DESC;

-- Unique index required for REFRESH CONCURRENTLY (mirrors 008/023).
CREATE UNIQUE INDEX idx_mv_latest_states_identity
    ON core.mv_latest_agent_states (identity_id);

-- Lookups by agent_id (mirrors 008/023).
CREATE INDEX idx_mv_latest_states_agent
    ON core.mv_latest_agent_states (agent_id);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (38, 'agent_state_risk_score', NOW())
ON CONFLICT (version) DO NOTHING;
