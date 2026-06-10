-- 043_identities_shadow.sql
--
-- Wave 3 §8.1 (docs/proposals/beam-wave-3-handler-dispatch.md): shadow table
-- for core.identities during the Wave 3 shadow window. The BEAM shadow writer
-- (Wave 3 implementation — NOT this migration; nothing writes here yet)
-- dual-writes PATH-3 fresh mints into this table. The comparator at
-- scripts/ops/wave-3-shadow-divergence-check.sql full-outer-joins shadow
-- against canonical and emits coordination_failure.beam_python_boundary.
-- shadow_divergence per divergent row.
--
-- FK decision (§8.1, documented inline as required): LIKE ... INCLUDING ALL
-- does NOT copy foreign-key constraints in PostgreSQL, and we deliberately
-- leave FKs OFF the shadow table — it is a write-only audit replica, not a
-- referential target. FKs would impose write-ordering constraints on the
-- shadow writer that the comparator does not need.
--
-- Sequence note: INCLUDING ALL copies column DEFAULTs, so identity_id's
-- default remains nextval('core.identities_identity_id_seq') — the SAME
-- sequence as canonical. Accepted and documented rather than hidden: the
-- shadow writer always supplies identity_id explicitly (copied from the
-- canonical write), so the shared-sequence default is never exercised in
-- normal operation, and a stray defaulted insert cannot collide with
-- canonical values (one shared counter).
--
-- Schema drift on core.identities requires a paired shadow update;
-- db/postgres/schema_drift_check.sh (this PR) fails when the column shapes
-- diverge (modulo shadow_write_at).

CREATE TABLE IF NOT EXISTS core.identities_shadow (
    LIKE core.identities INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE core.identities_shadow IS
    'Wave 3 §8 shadow replica of core.identities. Write-only audit target for '
    'the BEAM shadow writer during the shadow window; no FKs by design; '
    'compared hourly by scripts/ops/wave-3-shadow-divergence-check.sql.';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (43, 'identities_shadow', NOW())
ON CONFLICT (version) DO NOTHING;
