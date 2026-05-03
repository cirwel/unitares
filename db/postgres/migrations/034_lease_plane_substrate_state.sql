-- Migration 034: substrate_state columns on lease_plane.surface_leases
--
-- Implements RFC v0.11 §7.13 (Resident heartbeat surface — substrate-state
-- separation from monitor_decision). Triggered by 2026-05-01 Steward
-- auto-pause incident (KG discovery 2026-05-03T20:12:19; root-cause
-- council 2026-05-03).
--
-- Adds two NULLABLE columns to lease_plane.surface_leases that let
-- residents report substrate observations on a path STRUCTURALLY separate
-- from monitor_decision's pause/proceed gate. Existing rows have both
-- columns NULL and remain valid under all CHECK constraints; existing
-- traffic continues with no changes.
--
-- Enforcement (4 CHECK constraints, per RFC §7.13.5):
--
--   1. substrate_state_observed_pair_coherent — both columns NULL or both set
--   2. substrate_state_only_on_resident_kind — substrate_state forbidden on
--      non-resident leases (surface_kind = 'resident')
--   3. substrate_state_must_be_object — when set, substrate_state MUST be
--      a JSON object (not array, string, number, or bare null)
--   4. substrate_state_has_sensor_status — when set, substrate_state MUST
--      have a 'sensor' object sub-key with a string-typed 'status' field
--      whose value is one of 'healthy' | 'degraded' | 'failed'. Closes the
--      doc-lie pattern at the value-vocabulary level (the 2026-05-01
--      incident's layer-1 was a CPU sensor labeled "Memory headroom" with
--      no DB-side enforcement of the channel name OR its values).
--
-- Index: partial index on substrate_state_observed_at DESC where
-- substrate_state IS NOT NULL. Supports "show me all residents with stale
-- substrate state" freshness queries. The substrate_state_must_be_object
-- CHECK makes "IS NOT NULL → is object" a structural guarantee, so the
-- index doesn't need a redundant jsonb_typeof guard.
--
-- Determinism note (RFC §7.13.5 v0.11.3): when a write violates two CHECKs
-- simultaneously, PG evaluates check constraints in OID order. Same-
-- transaction definition order matches OID order, but the SQL standard
-- does not guarantee this across migration retries / restores. Callers
-- MUST tolerate any of the four constraint names appearing in the typed
-- error response; tests verify membership in the set, not a specific name.

ALTER TABLE lease_plane.surface_leases
    ADD COLUMN IF NOT EXISTS substrate_state             jsonb       NULL,
    ADD COLUMN IF NOT EXISTS substrate_state_observed_at timestamptz NULL;

-- Pair-coherence CHECK. Wrapped in DO block for idempotency (PG has no
-- ADD CONSTRAINT IF NOT EXISTS syntax; ensure_test_database_schema re-runs
-- migrations against existing databases).
DO $$ BEGIN
    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT substrate_state_observed_pair_coherent
        CHECK (
            (substrate_state IS NULL AND substrate_state_observed_at IS NULL)
            OR
            (substrate_state IS NOT NULL AND substrate_state_observed_at IS NOT NULL)
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Resident-kind-only CHECK. surface_kind is the migration-026 generated
-- column (split_part(surface_id, ':', 1)), so this CHECK is structurally
-- bound to surface_id and cannot drift.
DO $$ BEGIN
    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT substrate_state_only_on_resident_kind
        CHECK (
            substrate_state IS NULL OR surface_kind = 'resident'
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- jsonb-object-only CHECK. Rejects array / string / number / bare null.
-- Required so the freshness index predicate can rely on structural shape.
DO $$ BEGIN
    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT substrate_state_must_be_object
        CHECK (
            substrate_state IS NULL OR jsonb_typeof(substrate_state) = 'object'
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Sensor sub-key CHECK with vocabulary enforcement. Uses '->' (not '->>')
-- for jsonb_typeof checking to avoid the text-cast hole where a numeric
-- status value would be silently cast to a string.
--
-- IS NOT NULL guards on `sensor` and `sensor -> 'status'` are LOAD-BEARING:
-- without them, a missing sub-key produces jsonb_typeof(NULL) = NULL, and
-- PG CHECK constraints PASS on NULL evaluation. Explicit IS NOT NULL forces
-- absence to evaluate as false, which is what causes the CHECK to fire.
-- (Confirmed via psql: jsonb_typeof('{"E":0.5}'::jsonb -> 'sensor') returns
-- NULL, not 'undefined' or anything comparable to 'object'.)
DO $$ BEGIN
    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT substrate_state_has_sensor_status
        CHECK (
            substrate_state IS NULL
            OR (
                (substrate_state -> 'sensor') IS NOT NULL
                AND jsonb_typeof(substrate_state -> 'sensor') = 'object'
                AND (substrate_state -> 'sensor' -> 'status') IS NOT NULL
                AND jsonb_typeof(substrate_state -> 'sensor' -> 'status') = 'string'
                AND (substrate_state -> 'sensor' ->> 'status')
                    IN ('healthy', 'degraded', 'failed')
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Freshness index for "all residents with stale substrate state" queries.
CREATE INDEX IF NOT EXISTS idx_surface_leases_substrate_freshness
    ON lease_plane.surface_leases (substrate_state_observed_at DESC)
    WHERE substrate_state IS NOT NULL;

COMMENT ON COLUMN lease_plane.surface_leases.substrate_state             IS 'RFC §7.13: resident substrate observation. JSON object with required sensor.status sub-key (healthy|degraded|failed). MUST NOT be consumed by automated decision classes that can pause an agent.';
COMMENT ON COLUMN lease_plane.surface_leases.substrate_state_observed_at IS 'RFC §7.13: wall-clock when the resident sampled its substrate (distinct from last_heartbeat_at, which is when the lease was refreshed).';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (34, 'lease_plane_substrate_state', NOW())
ON CONFLICT (version) DO NOTHING;
