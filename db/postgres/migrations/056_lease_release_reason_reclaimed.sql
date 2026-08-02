-- 056_lease_release_reason_reclaimed.sql
--
-- Incident (2026-08-01): a Postgres stall pushed the lease plane past
-- Sentinel's 2s client budget on BOTH an acquire and its recovery retry
-- (PR #1443) while the first attempt's INSERT had already committed. The
-- client discarded the attempt's holder uuid, every later tick minted a fresh
-- uuid that saw only held_by_other, and ForcedReleasePoller starved for 1h49m
-- (216 blocked ticks) until an operator force-released the orphan. Three more
-- surfaces (steward, steward_eisv_sync, ship_sh_claude/adjudication-evidence)
-- were orphaned the same way in the same window.
--
-- The client-side fix teaches residents to remember the holder uuids of
-- acquire attempts whose responses were lost, recognize a later held_by_other
-- that names one of them as their OWN stranded lease, and release it
-- (elixir/sentinel: LeaseAdvisory + LeaseReclaim). Those releases carry
-- release_reason='reclaimed_lost_acquire' rather than 'normal', because
-- span-based analyses treat release_reason='normal' as "a live holder
-- released its own in-hand lease" — the property the 90-day
-- legitimate-long-hold dataset rests on (it refuted the auto-renew-cap
-- design). A reclaimed orphan's span since acquire is anything but a
-- legitimate hold; give it its own label.
--
-- Two gates must agree on the reason vocabulary:
--   * the router-level enum (http_router.ex extract_release_params/1) — the
--     first gate, returns 422 schema_invalid;
--   * this CHECK constraint (024_lease_plane.sql) — the schema backstop.
-- Extending only the router makes the UPDATE fail the CHECK and surface as a
-- 503. The client deliberately does NOT fall back to 'normal' on a 503 (only
-- on the unambiguous 422 from an old router), so a plane running new router
-- code over an unapplied migration fails reclaims LOUDLY — a repeating
-- warning plus the LeaseStarvation finding — instead of silently mislabeling
-- reclaimed-orphan spans as 'normal' forever. Apply this migration BEFORE
-- deploying the router change.

BEGIN;

ALTER TABLE lease_plane.surface_leases
    DROP CONSTRAINT surface_leases_release_reason_check;

ALTER TABLE lease_plane.surface_leases
    ADD CONSTRAINT surface_leases_release_reason_check CHECK (
        release_reason IS NULL OR release_reason IN (
            'normal',
            'down_local',
            'reaped_after_supervisor_failed',
            'reaped_local_ttl',
            'reaped_remote_ttl',
            'handoff',
            'forced',
            'reclaimed_lost_acquire'
        )
    );

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (56, 'lease_release_reason_reclaimed', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
