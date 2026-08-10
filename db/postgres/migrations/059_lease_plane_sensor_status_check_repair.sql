-- 059_lease_plane_sensor_status_check_repair.sql
--
-- Repairs a partially-applied migration 034. The governance DB carries only 3
-- of 034's 4 CHECK constraints on lease_plane.surface_leases:
--
--     present: substrate_state_must_be_object
--              substrate_state_observed_pair_coherent
--              substrate_state_only_on_resident_kind
--     MISSING: substrate_state_has_sensor_status
--
-- Root cause (found 2026-08-10). core.schema_migrations registers version 34
-- applied at 2026-05-03T21:28:22Z, but 37bdf999 — the only commit that has ever
-- touched 034 — landed at 2026-05-03T22:42:14Z, 74 minutes LATER. So prod was
-- migrated from an in-progress working tree; the fourth CHECK was written into
-- the file afterwards but before commit, and apply_migrations.py has skipped
-- version 34 ever since (it plans by registered version, never by file
-- content). The gap has been open since 2026-05-03.
--
-- Why it stayed invisible: tests/test_lease_plane_substrate_state.py asserts
-- this exact constraint fires (gate (e)), and it passes — because
-- ensure_test_database_schema re-executes 034 IN FULL against the test DB, so
-- the test population has a constraint the production population does not. No
-- test can see this class of drift; it is a property of the deployed database,
-- not of the code.
--
-- Confirmed live before writing this: POST /v1/lease/renew with
-- substrate_state = {"ok": true} — no 'sensor' key at all — returned 200 and
-- persisted. That is precisely the doc-lie pattern 034's header says this
-- constraint exists to close (the 2026-05-01 incident: a CPU sensor labeled
-- "Memory headroom", enforced nowhere).
--
-- Backfill safety: at authoring time 135 rows carried substrate_state and 0
-- violated the predicate below, so this adds cleanly with no data repair. The
-- ALTER re-validates every existing row regardless, so a row that drifted in
-- since fails the migration LOUDLY rather than being silently grandfathered.
--
-- A repo-wide sweep of every ADD CONSTRAINT across db/postgres/migrations
-- against pg_constraint found this to be the ONLY such gap; the other absent
-- names are intentional drops (discoveries_severity_check,
-- discoveries_status_check). Scope is deliberately limited to that one repair.

BEGIN;

-- Definition copied verbatim from 034 — same name, same predicate — so the two
-- files cannot drift into disagreeing about what the constraint means.
--
-- The IS NOT NULL guards are load-bearing: a missing sub-key yields
-- jsonb_typeof(NULL) = NULL, and a CHECK constraint PASSES on a NULL result, so
-- without them absence would satisfy the constraint instead of failing it.
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

-- Post-condition. The failure this migration repairs was a constraint that did
-- not exist while the migration claiming it reported success, so this file
-- refuses to register itself unless the constraint is actually on the table.
-- Turns any future silent no-op into a failed migration instead of a clean exit.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'lease_plane.surface_leases'::regclass
          AND contype = 'c'
          AND conname = 'substrate_state_has_sensor_status'
    ) THEN
        RAISE EXCEPTION
            'substrate_state_has_sensor_status absent after ADD CONSTRAINT — '
            'migration 059 refuses to register a repair it did not make';
    END IF;
END $$;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (59, 'lease_plane_sensor_status_check_repair', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
