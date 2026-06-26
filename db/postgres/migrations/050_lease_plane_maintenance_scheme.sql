-- 050_lease_plane_maintenance_scheme.sql
--
-- Add `maintenance:/` to the lease-plane surface grammar.
--
-- `resident:/` remains reserved for actual resident lifecycle/cycle/presence
-- surfaces. Maintenance jobs that clean or repair shared operational state
-- (branch hygiene, worktree reaping, future janitorial jobs) use
-- `maintenance:/...` so enforcement and telemetry can target cleanup work
-- without implying a resident identity or proxy holder.
--
-- `surface_kind` is still the migration-026 generated column
-- (split_part(surface_id, ':', 1)), so `maintenance:/...` derives
-- surface_kind='maintenance' automatically.

INSERT INTO lease_plane.surface_kind_catalog (surface_kind, description)
VALUES
    ('maintenance', 'cleanup/repair coordination surfaces; not resident identity')
ON CONFLICT (surface_kind) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'surface_id_grammar'
          AND conrelid = 'lease_plane.surface_leases'::regclass
    ) THEN
        ALTER TABLE lease_plane.surface_leases DROP CONSTRAINT surface_id_grammar;
    END IF;

    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT surface_id_grammar
        CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|maintenance:/|capture:/|td:/|agent:/)');
END $$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (50, 'lease_plane_maintenance_scheme', NOW())
ON CONFLICT (version) DO NOTHING;
