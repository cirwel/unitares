-- 020_progress_flat_telemetry.sql
--
-- Phase 1 telemetry tables for the resident-progress probe (see
-- docs/superpowers/specs/2026-04-25-resident-progress-detection-design.md).
-- Append-only; the probe never updates or deletes rows.
--
-- Note: planned as 017 but 017 was taken by substrate_claims (S19 PR1).

CREATE TABLE IF NOT EXISTS progress_flat_snapshots (
    id                     bigserial PRIMARY KEY,
    probe_tick_id          uuid NOT NULL,
    ticked_at              timestamptz NOT NULL,
    resident_label         text NOT NULL,
    resident_uuid          uuid,                  -- null for probe_self and unresolved labels
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

-- NOTE: the phantom that ran out-of-band registered at slot 18. This file is
-- the canonical master copy at slot 20. Applying it on prod inserts (20, ...)
-- alongside the existing phantom (18, ...) — two registry rows, one schema.
INSERT INTO core.schema_migrations (version, name)
VALUES (20, 'progress flat telemetry tables')
ON CONFLICT (version) DO NOTHING;
