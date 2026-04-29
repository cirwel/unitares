-- 022_bootstrap_synthetic_state.sql  (renumbered from 018)
--
-- Phase 1 of onboard-bootstrap-checkin.md (v2.1).
--
-- Adds a `synthetic` boolean column to core.agent_state so the server can
-- write labeled "bootstrap" check-in rows on onboard without laundering them
-- into measured-state code paths. The column is the load-bearing filter key;
-- `state_json.source = "bootstrap"` is descriptive metadata.
--
-- Schema-only migration. No handler code, no filter-site changes. Subsequent
-- phases (per §9 of the proposal) add: handler that writes the row, every
-- read-path filter (`synthetic = false` default), hook integration, and the
-- bootstrapped-but-silent observable surface.
--
-- Rollback shape: drop the unique index, drop the partial index, drop the
-- column. The matview can be re-DROPped/CREATEd from migration 008 verbatim.

-- 1. Add the column. Default false means every existing row is correctly
--    classified as measured. ADD COLUMN with a constant default is instant
--    on PostgreSQL@17 (catalog-only, no row rewrite).
ALTER TABLE core.agent_state
    ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT false;

-- 2. Partial index on the dominant query shape (most-recent measured state
--    per identity). Skips synthetic rows entirely so the filter pays no
--    seek cost.
CREATE INDEX IF NOT EXISTS idx_agent_state_synthetic_partial
    ON core.agent_state (identity_id, recorded_at DESC)
    WHERE synthetic = false;

-- 3. At-most-one bootstrap row per identity. Closes the concurrent-onboard
--    race (two parallel hooks for the same UUID) at the DB layer rather than
--    relying on application-level locks. Bootstrap writes that lose the race
--    catch the unique-violation and return the winning row's state_id with
--    `bootstrap.written: false`.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_state_one_bootstrap_per_identity
    ON core.agent_state (identity_id)
    WHERE synthetic = true;

-- 4. Rebuild the dashboard materialized view to project the new column and
--    keep bootstrap rows out of the rowset. This intentionally matches the
--    terminal measured-only definition so a failed/manual stop before 023
--    cannot expose synthetic rows through the matview. The existing fallback
--    at src/db/mixins/state.py:103 covers the brief drop/create window
--    (try/except falls through to a base-table query).
DROP MATERIALIZED VIEW IF EXISTS core.mv_latest_agent_states;

CREATE MATERIALIZED VIEW core.mv_latest_agent_states AS
SELECT DISTINCT ON (s.identity_id)
       s.state_id, s.identity_id, i.agent_id, s.recorded_at,
       s.entropy, s.integrity, s.stability_index, s.volatility,
       s.regime, s.coherence, s.state_json, s.synthetic
FROM core.agent_state s
JOIN core.identities i ON i.identity_id = s.identity_id
WHERE s.synthetic = false
ORDER BY s.identity_id, s.recorded_at DESC;

-- Unique index required for REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX idx_mv_latest_states_identity
    ON core.mv_latest_agent_states (identity_id);

-- For lookups by agent_id (mirrors migration 008).
CREATE INDEX idx_mv_latest_states_agent
    ON core.mv_latest_agent_states (agent_id);

-- Register the migration.
-- NOTE: renumbered 018→022. Slot 18 is occupied in prod by the phantom
-- 'progress flat telemetry tables' entry (commit f7f71723, out-of-band apply).
-- The synthetic column was hot-fixed inline on 2026-04-26; the ALTER TABLE
-- and CREATE INDEX IF NOT EXISTS below are safe no-ops on prod.
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (22, 'bootstrap_synthetic_state', NOW())
ON CONFLICT (version) DO NOTHING;
