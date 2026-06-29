-- 025_lease_plane_invariants.sql
--
-- Surface lease plane v0 — close council-found gaps.
--
-- Council finding 2 (verifier confirmed via direct UPDATE):
-- migration 024's enforce_immutable_lease_fields trigger only locked
-- holder_kind, holder_class, original_ttl_s. surface_id, surface_kind,
-- holder_agent_uuid, and acquired_at were silently mutable on a live
-- lease row, even though the idempotency contract and the active-unique
-- partial index treat the (surface_id, holder_agent_uuid) pair as
-- load-bearing identity.
--
-- Council finding (dialectic): RFC §7.8 ack-pass commits the v0 lease
-- plane to substrate-earned-class but flagged "provisional" until ≥30d
-- stable operation. That flag was invisible at the schema boundary —
-- surfaces it now as a first-class column so external readers cannot
-- mistake v0 for a settled invariant.

-- 1. Add earned_status column to surface_leases. v0 acquisitions default
--    to 'provisional'; promotion to 'earned' is a future migration after
--    ≥30d stable operation per RFC §7.8.
ALTER TABLE lease_plane.surface_leases
    ADD COLUMN IF NOT EXISTS earned_status text NOT NULL DEFAULT 'provisional'
    CHECK (earned_status IN ('provisional', 'earned'));

-- 2. Add earned_status to lease_plane_events for audit-trail visibility.
--    Nullable: some event_types (conflict_held_by_other, service_unavailable)
--    record a refusal where no lease was created.
ALTER TABLE lease_plane.lease_plane_events
    ADD COLUMN IF NOT EXISTS earned_status text
    CHECK (earned_status IS NULL OR earned_status IN ('provisional', 'earned'));

-- 3. Replace the immutability trigger function with a complete guard set.
--    Original 024 function locked 3 fields; this expands to 8.
CREATE OR REPLACE FUNCTION lease_plane.enforce_immutable_lease_fields()
RETURNS trigger AS $$
BEGIN
    IF NEW.holder_kind IS DISTINCT FROM OLD.holder_kind THEN
        RAISE EXCEPTION 'holder_kind is immutable per lease_id; release+reacquire to change';
    END IF;
    IF NEW.holder_class IS DISTINCT FROM OLD.holder_class THEN
        RAISE EXCEPTION 'holder_class is immutable per lease_id';
    END IF;
    IF NEW.original_ttl_s IS DISTINCT FROM OLD.original_ttl_s THEN
        RAISE EXCEPTION 'original_ttl_s is immutable per lease_id; renew uses this fixed value';
    END IF;
    IF NEW.surface_id IS DISTINCT FROM OLD.surface_id THEN
        RAISE EXCEPTION 'surface_id is immutable per lease_id; lease identity is bound to (surface_id, holder)';
    END IF;
    IF NEW.surface_kind IS DISTINCT FROM OLD.surface_kind THEN
        RAISE EXCEPTION 'surface_kind is immutable per lease_id';
    END IF;
    IF NEW.holder_agent_uuid IS DISTINCT FROM OLD.holder_agent_uuid THEN
        RAISE EXCEPTION 'holder_agent_uuid is immutable per lease_id; handoff uses release+reacquire, not in-place update';
    END IF;
    IF NEW.acquired_at IS DISTINCT FROM OLD.acquired_at THEN
        RAISE EXCEPTION 'acquired_at is immutable per lease_id';
    END IF;
    IF NEW.earned_status IS DISTINCT FROM OLD.earned_status THEN
        RAISE EXCEPTION 'earned_status is immutable per lease_id; promote new acquisitions, not historical rows';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (25, 'lease_plane_invariants', NOW())
ON CONFLICT (version) DO NOTHING;
