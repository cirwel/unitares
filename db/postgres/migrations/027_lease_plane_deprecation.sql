-- 027_lease_plane_deprecation.sql
--
-- Phase A persistence substrate for surface-kind deprecation per RFC v0.8 §7.11.1.
--
-- Adds two tables:
--   lease_plane.surface_kind_catalog — canonical registry of allowed scheme prefixes.
--                                       Seeded with the 5 v0 schemes.
--   lease_plane.deprecated_schemes  — first-class state for the §7.11 4-phase
--                                      deprecation procedure. FK to catalog so
--                                      deprecation can only target a registered kind.
--
-- The migration also extends lease_plane.lease_plane_events.event_type CHECK to
-- accept the three new deprecation event types ('lease.deprecation_marked',
-- 'lease.deprecation_swept', 'lease.deprecation_migrated') per §7.11.3.
--
-- Idempotent: tables guarded by IF NOT EXISTS; INSERT seed via ON CONFLICT;
-- event_type CHECK rebuilt via DROP+ADD inside a DO block.

CREATE TABLE IF NOT EXISTS lease_plane.surface_kind_catalog (
    surface_kind text PRIMARY KEY,
    description  text,
    added_at     timestamptz NOT NULL DEFAULT now()
);

INSERT INTO lease_plane.surface_kind_catalog (surface_kind, description)
VALUES
    ('file',      'repo file paths; canonicalization rules in RFC §7.12.1'),
    ('dialectic', 'dialectic session IDs (lowercase hex UUID)'),
    ('resident',  'resident lifecycle handles'),
    ('capture',   'calibration capture windows; member list canonicalized lexically'),
    ('td',        'TouchDesigner regions; reserved, not implemented v0')
ON CONFLICT (surface_kind) DO NOTHING;

CREATE TABLE IF NOT EXISTS lease_plane.deprecated_schemes (
    surface_kind          text PRIMARY KEY
        REFERENCES lease_plane.surface_kind_catalog(surface_kind)
        ON DELETE RESTRICT,
    deprecation_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    marked_deprecated_at  timestamptz NOT NULL DEFAULT now(),
    marked_by_session_id  text NOT NULL,
    drain_window_days     int NOT NULL DEFAULT 30
        CHECK (drain_window_days > 0 AND drain_window_days <= 90),
    sweep_started_at      timestamptz,
    sweep_completed_at    timestamptz,
    check_migrated_at     timestamptz
);

CREATE INDEX IF NOT EXISTS deprecated_schemes_in_progress
    ON lease_plane.deprecated_schemes (surface_kind)
    WHERE check_migrated_at IS NULL;

-- Extend lease_plane_events.event_type CHECK to include §7.11.3 deprecation events.
DO $$
DECLARE
    has_deprecation_marked bool;
BEGIN
    -- Detect prior application: try inserting and rolling back is one option,
    -- but cleaner is to inspect the constraint's check clause.
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'lease_plane'
          AND t.relname = 'lease_plane_events'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%lease.deprecation_marked%'
    ) INTO has_deprecation_marked;

    IF NOT has_deprecation_marked THEN
        -- Drop the existing event_type CHECK; rebuild with deprecation events added.
        ALTER TABLE lease_plane.lease_plane_events
            DROP CONSTRAINT IF EXISTS lease_plane_events_event_type_check;
        ALTER TABLE lease_plane.lease_plane_events
            ADD CONSTRAINT lease_plane_events_event_type_check
            CHECK (
                event_type IN (
                    'acquire',
                    'renew',
                    'release',
                    'heartbeat',
                    'handoff_offer',
                    'handoff_accept',
                    'conflict_held_by_other',
                    'reaped_remote_ttl',
                    'reaped_local_ttl',
                    'down_local',
                    'forced',
                    'service_unavailable',
                    'lease.deprecation_marked',
                    'lease.deprecation_swept',
                    'lease.deprecation_migrated'
                )
            );
    END IF;
END $$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (27, 'lease_plane_deprecation', NOW())
ON CONFLICT (version) DO NOTHING;
