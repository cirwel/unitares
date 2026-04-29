-- 022_reconcile_schema_migration_drift.sql
--
-- Repairs the 2026-04-26 production drift where an out-of-band
-- progress-flat migration claimed registry slot 18 before
-- 018_bootstrap_synthetic_state.sql was applied. This migration is
-- intentionally idempotent and safe to run on:
--
--   * fresh databases that already ran 018, 019, 020, 021 in order
--   * production-like databases where version 18 is incorrectly registered
--     as "progress flat telemetry tables"
--   * databases that received the synthetic-column hot-fix but not the
--     registry/table migrations around it
--
-- It does not delete history. It corrects the slot-18 label only when it
-- matches the known phantom name, registers progress-flat as version 20, and
-- brings the missing idempotent DDL forward under version 22.

-- Migration 014: ensure epoch 2 is registered.
INSERT INTO core.epochs (epoch, started_at, reason, started_by)
VALUES (
    2,
    '2026-03-29 13:54:24-06',
    'behavioral EISV replaces ODE dynamics - old state data incompatible (v2.9.0 / commit cbaaed95)',
    'backfill'
)
ON CONFLICT (epoch) DO NOTHING;

-- Migration 015: process binding audit table and policy columns.
CREATE TABLE IF NOT EXISTS core.agent_process_bindings (
    id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES core.agents(id) ON DELETE CASCADE,
    host_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    pid_start_time DOUBLE PRECISION NOT NULL,
    transport TEXT NOT NULL DEFAULT 'unknown',
    ppid INTEGER NULL,
    tty TEXT NULL,
    anchor_path_hash TEXT NULL,
    client_session_id TEXT NULL,
    onboard_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stale_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_agent_process_binding
        UNIQUE (agent_id, host_id, pid, pid_start_time, transport)
);

CREATE INDEX IF NOT EXISTS idx_apb_agent_live
    ON core.agent_process_bindings (agent_id, stale_at)
    WHERE stale_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_apb_last_seen
    ON core.agent_process_bindings (last_seen)
    WHERE stale_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_apb_agent_recency
    ON core.agent_process_bindings (agent_id, last_seen DESC);

ALTER TABLE core.agents
    ADD COLUMN IF NOT EXISTS allow_rebind_after_exit BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE core.agents
    ADD COLUMN IF NOT EXISTS allow_concurrent_contexts BOOLEAN NOT NULL DEFAULT FALSE;

-- Migration 016: same-host ppid confidence signal.
ALTER TABLE core.agent_process_bindings
    ADD COLUMN IF NOT EXISTS same_host_ppid_consistent BOOLEAN NULL;

-- Migration 018/019: synthetic state column and measured-only latest-state matview.
ALTER TABLE core.agent_state
    ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_agent_state_synthetic_partial
    ON core.agent_state (identity_id, recorded_at DESC)
    WHERE synthetic = false;

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_state_one_bootstrap_per_identity
    ON core.agent_state (identity_id)
    WHERE synthetic = true;

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

CREATE UNIQUE INDEX idx_mv_latest_states_identity
    ON core.mv_latest_agent_states (identity_id);

CREATE INDEX idx_mv_latest_states_agent
    ON core.mv_latest_agent_states (agent_id);

-- Migration 020: progress-flat telemetry tables, registered under the correct slot.
CREATE TABLE IF NOT EXISTS progress_flat_snapshots (
    id                     bigserial PRIMARY KEY,
    probe_tick_id          uuid NOT NULL,
    ticked_at              timestamptz NOT NULL,
    resident_label         text NOT NULL,
    resident_uuid          uuid,
    source                 text NOT NULL,
    metric_value           integer,
    window_seconds         integer,
    threshold              integer,
    metric_below_threshold boolean,
    heartbeat_alive        boolean,
    candidate              boolean NOT NULL DEFAULT false,
    suppressed_reason      text,
    error_details          jsonb,
    liveness_inputs        jsonb,
    loop_detector_state    jsonb
);

CREATE INDEX IF NOT EXISTS idx_pfs_ticked_at
    ON progress_flat_snapshots (ticked_at DESC);

CREATE INDEX IF NOT EXISTS idx_pfs_label_ticked
    ON progress_flat_snapshots (resident_label, ticked_at DESC);

CREATE INDEX IF NOT EXISTS idx_pfs_tick_id
    ON progress_flat_snapshots (probe_tick_id);

CREATE TABLE IF NOT EXISTS resident_progress_pulse (
    id            bigserial PRIMARY KEY,
    resident_uuid uuid NOT NULL,
    metric_name   text NOT NULL,
    value         integer NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rpp_uuid_recorded
    ON resident_progress_pulse (resident_uuid, recorded_at DESC);

-- Registry repair. Only rewrite slot 18 when it is the known phantom entry.
UPDATE core.schema_migrations
SET name = 'bootstrap_synthetic_state'
WHERE version = 18
  AND name = 'progress flat telemetry tables';

INSERT INTO core.schema_migrations (version, name)
VALUES
    (14, 'seed_epoch_2'),
    (15, 'agent_process_bindings'),
    (16, 'same_host_ppid_consistent'),
    (18, 'bootstrap_synthetic_state'),
    (19, 'matview_measured_only'),
    (20, 'progress flat telemetry tables'),
    (22, 'reconcile_schema_migration_drift')
ON CONFLICT (version) DO NOTHING;
