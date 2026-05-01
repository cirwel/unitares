-- 028_lease_plane_trigger_fix.sql
--
-- Fixes a PR 1 oversight surfaced during PR 3a TDD:
-- migration 025's enforce_immutable_lease_fields() trigger guards
-- NEW.surface_kind IS DISTINCT FROM OLD.surface_kind, but after migration 026
-- converted surface_kind to a STORED generated column, BEFORE UPDATE triggers
-- see NULL for the generated column (it's computed AFTER the trigger fires).
--
-- Result: ANY UPDATE on surface_leases (including the §7.11 deprecation sweep)
-- raises 'surface_kind is immutable per lease_id' even though surface_kind
-- isn't actually changing.
--
-- Fix: drop the surface_kind check from the trigger. surface_id is already
-- guarded for immutability; since surface_kind is now derived from
-- split_part(surface_id, ':', 1), guarding surface_id transitively guards
-- surface_kind.
--
-- The check on holder_kind, holder_class, original_ttl_s, holder_agent_uuid,
-- and acquired_at remains.

CREATE OR REPLACE FUNCTION lease_plane.enforce_immutable_lease_fields()
RETURNS trigger AS $$
BEGIN
    IF NEW.surface_id IS DISTINCT FROM OLD.surface_id THEN
        RAISE EXCEPTION 'surface_id is immutable per lease_id; lease identity is bound to (surface_id, holder)';
    END IF;
    -- surface_kind check removed (PR 3a / migration 028): post-026 it is a
    -- generated column derived from surface_id; the surface_id guard above
    -- transitively enforces surface_kind immutability.
    IF NEW.holder_agent_uuid IS DISTINCT FROM OLD.holder_agent_uuid THEN
        RAISE EXCEPTION 'holder_agent_uuid is immutable per lease_id';
    END IF;
    IF NEW.holder_kind IS DISTINCT FROM OLD.holder_kind THEN
        RAISE EXCEPTION 'holder_kind is immutable per lease_id; release+reacquire to change';
    END IF;
    IF NEW.holder_class IS DISTINCT FROM OLD.holder_class THEN
        RAISE EXCEPTION 'holder_class is immutable per lease_id';
    END IF;
    IF NEW.original_ttl_s IS DISTINCT FROM OLD.original_ttl_s THEN
        RAISE EXCEPTION 'original_ttl_s is immutable per lease_id; renew uses this fixed value';
    END IF;
    IF NEW.acquired_at IS DISTINCT FROM OLD.acquired_at THEN
        RAISE EXCEPTION 'acquired_at is immutable per lease_id';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (28, 'lease_plane_trigger_fix', NOW())
ON CONFLICT (version) DO NOTHING;
