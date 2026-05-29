-- Migration 041: Wave 3a coordination measurement channel
--
-- Implements docs/proposals/beam-wave-3a-read-only-handlers.md §4.3:
-- "audit.coordination_measurements ships as PR #2 of this wave, before §4.1
--  or §4.2 can be evaluated."
--
-- §4.1 (HTTP transport cost under contention, 5x baseline) and §4.2 (503 /
-- fallback rate sliding window) both read from this surface. Wave 3a writes
-- `measurement_type = 'measurement.wave_3a.request'` for every probe call;
-- Wave 3b/3c write distinct `measurement_type` values into the same table.
--
-- Schema is deliberately minimal per §4.3 — `measurement_type`-keyed so later
-- waves extend `meta` JSONB without migrating.
--
-- Partitioning choice: flat table, not partitioned. The probe surface is
-- low-volume relative to audit.coordination_events (one row per probe call,
-- not per agent-state mutation); the schema spec in §4.3 does not call for
-- partitioning. If volume grows once Wave 3b/3c land, a follow-up migration
-- can swap to RANGE(recorded_at) without changing the column shape.

CREATE TABLE IF NOT EXISTS audit.coordination_measurements (
    id               BIGSERIAL    PRIMARY KEY,
    recorded_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    measurement_type TEXT         NOT NULL,
    endpoint         TEXT         NOT NULL,
    elapsed_ms       INTEGER      NOT NULL,
    status           TEXT         NOT NULL,
    payload_bytes    INTEGER      NULL,
    meta             JSONB        NULL
);

-- meta MUST be a JSON object when present (mirrors the audit.coordination_events
-- payload/context discipline at migration 035 lines 74-83). Readers can rely
-- on meta->>'<key>' shape.
DO $$ BEGIN
    ALTER TABLE audit.coordination_measurements
        ADD CONSTRAINT coordination_measurements_meta_object
        CHECK (meta IS NULL OR jsonb_typeof(meta) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- measurement_type namespace CHECK. Mirrors migration 035's namespace pattern:
-- `measurement.<family>.<subtype>` (e.g. `measurement.wave_3a.request`).
-- Future waves extend by adding new family prefixes here, never by reusing
-- existing values.
DO $$ BEGIN
    ALTER TABLE audit.coordination_measurements
        ADD CONSTRAINT coordination_measurements_type_namespace
        CHECK (measurement_type ~ '^measurement(\.[a-z0-9_]+)+$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Indexes for the standard read patterns: §4.1/§4.2 window queries scan
-- (measurement_type, recorded_at DESC); per-endpoint p50/p99 lookups scan
-- (endpoint, recorded_at DESC) inside the Wave 3a measurement family.
CREATE INDEX IF NOT EXISTS idx_coord_meas_type_time
    ON audit.coordination_measurements (measurement_type, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_coord_meas_endpoint
    ON audit.coordination_measurements (endpoint, recorded_at DESC)
    WHERE measurement_type LIKE 'measurement.wave_3a.%';

COMMENT ON TABLE audit.coordination_measurements IS 'Wave 3a §4.3 measurement channel: one row per probe request (and, for Wave 3b/3c, per their measurement_type). Stop-sign denominators §4.1/§4.2 read from here. measurement_type extends by adding new dotted namespaces; never reuse or rename existing values.';

COMMENT ON COLUMN audit.coordination_measurements.measurement_type IS 'Dotted namespace. Wave 3a uses ''measurement.wave_3a.request''. Future waves extend the family prefix (measurement.wave_3b.*, etc.).';

COMMENT ON COLUMN audit.coordination_measurements.endpoint IS 'Route path string (e.g. ''/v1/probe/health_snapshot''). Indexed for per-endpoint p50/p99 queries.';

COMMENT ON COLUMN audit.coordination_measurements.status IS 'HTTP status code as text (''200'', ''401'', ''503''). Text rather than INTEGER so non-HTTP measurement_types (future waves) can carry domain-specific status codes without column reuse.';

COMMENT ON COLUMN audit.coordination_measurements.meta IS 'Free-form per-measurement_type context (auth-header presence, token-set flag, etc.). Per the §4.3 minimal-schema discipline, later waves extend meta JSONB rather than adding columns.';

-- Wave 3a extends coordination_events_event_type_namespace (migration 035) to
-- accept `coordination_failure.wave_3a.*`. The pre-existing CHECK regex
-- `^(coordination_failure)(\.[a-z_]+)+$` allows lowercase + underscore
-- segments only — it rejects `wave_3a` because of the digit. Wave 3a needs
-- the digit, so we relax the per-segment character class to `[a-z0-9_]+`.
-- This is a strict superset of the pre-existing acceptance set (every
-- existing Wave 0 / Wave 2 event_type still passes), so the constraint
-- change is backward compatible.
DO $$ BEGIN
    ALTER TABLE audit.coordination_events
        DROP CONSTRAINT IF EXISTS coordination_events_event_type_namespace;
    ALTER TABLE audit.coordination_events
        ADD CONSTRAINT coordination_events_event_type_namespace
        CHECK (event_type ~ '^(coordination_failure)(\.[a-z0-9_]+)+$');
END $$;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (41, 'wave3a_coordination_measurements', NOW())
ON CONFLICT (version) DO NOTHING;
