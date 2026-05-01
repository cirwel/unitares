-- 029_lease_plane_earned_status_guard.sql
--
-- Restores the `earned_status` immutability guard that migration 028 silently
-- dropped. Surfaced by the council pass on the PR 1-4 stack (dialectic voice
-- BLOCK-2): migration 025 originally guarded 8 fields against UPDATE; migration
-- 028 was authored to drop the surface_kind guard (it became a generated column
-- post-026) but the rewrite also dropped the earned_status guard as collateral
-- damage.
--
-- Without this guard, ANY UPDATE on surface_leases can silently flip earned_status
-- from 'provisional' to 'earned' — bypassing the substrate-earned-promotion
-- migration that RFC §7.8 anticipates.
--
-- This restores the check while preserving the relaxed surface_kind handling
-- from 028 (the surface_id immutability guard transitively protects the derived
-- surface_kind generated column).
--
-- Idempotent: CREATE OR REPLACE the function, no-op if already correct.

CREATE OR REPLACE FUNCTION lease_plane.enforce_immutable_lease_fields()
RETURNS trigger AS $$
BEGIN
    IF NEW.surface_id IS DISTINCT FROM OLD.surface_id THEN
        RAISE EXCEPTION 'surface_id is immutable per lease_id; lease identity is bound to (surface_id, holder)';
    END IF;
    -- surface_kind check intentionally omitted (post-028 it is a generated
    -- column derived from surface_id; the surface_id guard above transitively
    -- enforces surface_kind immutability).
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
    -- Restored in PR 5 / migration 029: earned_status guard (RFC §7.8).
    -- substrate-earned promotion requires explicit migration, not silent UPDATE.
    IF NEW.earned_status IS DISTINCT FROM OLD.earned_status THEN
        RAISE EXCEPTION 'earned_status is immutable per lease_id; substrate-earned promotion requires explicit migration (RFC §7.8)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (29, 'lease_plane_earned_status_guard', NOW())
ON CONFLICT (version) DO NOTHING;
