-- 042_lease_plane_agent_scheme.sql
--
-- Add `agent:/` to the surface_id grammar so ephemeral-agent PRESENCE surfaces
-- are legal. Ephemeral agents (spawned via the BEAM agent orchestrator,
-- elixir/agent_orchestrator) register an `agent:/<id>` surface as a liveness
-- record. The surface is unique per agent — it is presence, not a mutex — so it
-- is routed in the plane's acquire_for_surface to the `remote_heartbeat` path
-- (pure DB TTL row, NO auto-renewing LeaseHolder), exactly like `file://`. That
-- means an orphaned presence row reaps itself at `expires_at` instead of being
-- held open by a renewing holder for the BEAM process's lifetime.
--
-- Supersedes the migration-026 grammar regex. Idempotent: drops the existing
-- CHECK (if present) and re-adds with `agent:/` appended. `surface_kind` is the
-- migration-026 generated column (split_part(surface_id, ':', 1)), so an
-- `agent:/...` surface_id derives surface_kind='agent' automatically; the
-- migration-034 resident-only CHECK (substrate_state IS NULL OR
-- surface_kind='resident') is unaffected because agent presence rows leave
-- substrate_state NULL.

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
        CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/|agent:/)');
END $$;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (42, 'lease_plane_agent_scheme', NOW())
ON CONFLICT (version) DO NOTHING;
