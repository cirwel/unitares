-- 084_lease_principal_issuer_subject.sql
--
-- Persists the attested lease principal `(holder_issuer, holder_subject)`
-- alongside `holder_agent_uuid` on `lease_plane.surface_leases` (#1983).
--
-- Separating the attested principal from the per-attempt `holder_agent_uuid`
-- closes two gaps at once:
--   1. Multi-issuer trust becomes unambiguous: every attested lease row
--      records which trusted issuer vouched for its holder subject.
--   2. Resident leases can attest a durable governance principal while still
--      minting a fresh per-attempt `holder_agent_uuid` for contention/reclaim.
--
-- Existing rows retain `(NULL, NULL)` and remain valid under the pair-coherence
-- CHECK constraint. Both columns are immutable per lease_id once written.

BEGIN;

ALTER TABLE lease_plane.surface_leases
    ADD COLUMN IF NOT EXISTS holder_issuer  text NULL,
    ADD COLUMN IF NOT EXISTS holder_subject text NULL;

DO $$ BEGIN
    ALTER TABLE lease_plane.surface_leases
        ADD CONSTRAINT surface_leases_holder_principal_pair_coherent
        CHECK (
            (holder_issuer IS NULL AND holder_subject IS NULL)
            OR (
                holder_issuer IS NOT NULL
                AND octet_length(holder_issuer) BETWEEN 1 AND 256
                AND holder_issuer !~ E'[[:space:][:cntrl:]\u0080-\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'
                AND holder_subject IS NOT NULL
                AND octet_length(holder_subject) BETWEEN 1 AND 256
                AND holder_subject !~ E'[[:space:][:cntrl:]\u0080-\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_surface_leases_active_principal
    ON lease_plane.surface_leases (holder_issuer, holder_subject)
    WHERE released_at IS NULL AND holder_issuer IS NOT NULL;

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
    IF NEW.holder_issuer IS DISTINCT FROM OLD.holder_issuer THEN
        RAISE EXCEPTION 'holder_issuer is immutable per lease_id';
    END IF;
    IF NEW.holder_subject IS DISTINCT FROM OLD.holder_subject THEN
        RAISE EXCEPTION 'holder_subject is immutable per lease_id';
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
    -- Preserved from migration 029: earned_status guard (RFC §7.8).
    -- substrate-earned promotion requires explicit migration, not silent UPDATE.
    IF NEW.earned_status IS DISTINCT FROM OLD.earned_status THEN
        RAISE EXCEPTION 'earned_status is immutable per lease_id; substrate-earned promotion requires explicit migration (RFC §7.8)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON COLUMN lease_plane.surface_leases.holder_issuer  IS 'Verified lat.v1 attestation issuer (iss) that vouched for holder_subject. NULL for unattested/legacy leases; immutable per lease_id.';
COMMENT ON COLUMN lease_plane.surface_leases.holder_subject IS 'Verified lat.v1 attestation subject (sub) bound to this lease. NULL for unattested/legacy leases; immutable per lease_id.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (84, 'lease_principal_issuer_subject', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
