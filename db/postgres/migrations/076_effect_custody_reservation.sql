-- A committed reservation outlives DB connections and request processes.
-- Deploy with live file-write dispatch stopped and drained; old binaries must
-- not serve live writes alongside this admission protocol. See the runbook.
BEGIN;

ALTER TABLE lease_plane.surface_leases
    ADD COLUMN IF NOT EXISTS effect_fence TEXT;

CREATE INDEX IF NOT EXISTS idx_surface_leases_effect_fence
    ON lease_plane.surface_leases (effect_fence)
    WHERE effect_fence IS NOT NULL;

-- Guard every release path, including old binaries and direct SQL reapers.
-- Clearing a reservation is a separate, explicit operation. Never clear it
-- on a timeout: a disconnected executor may still be able to write bytes.
CREATE OR REPLACE FUNCTION lease_plane.guard_effect_custody()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.effect_fence IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'effect in progress'
                USING ERRCODE = '23514', CONSTRAINT = 'effect_custody_reserved';
        END IF;
        IF NEW.released_at IS NOT NULL
           OR NEW.lease_id IS DISTINCT FROM OLD.lease_id
           OR NEW.surface_id IS DISTINCT FROM OLD.surface_id
           OR NEW.holder_agent_uuid IS DISTINCT FROM OLD.holder_agent_uuid THEN
            RAISE EXCEPTION 'effect in progress'
                USING ERRCODE = '23514', CONSTRAINT = 'effect_custody_reserved';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS guard_effect_custody ON lease_plane.surface_leases;
CREATE TRIGGER guard_effect_custody
    BEFORE UPDATE OR DELETE ON lease_plane.surface_leases
    FOR EACH ROW EXECUTE FUNCTION lease_plane.guard_effect_custody();

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (76, 'effect_custody_reservation', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
