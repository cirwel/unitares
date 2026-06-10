-- 044_agents_shadow.sql
--
-- Wave 3 §8.1 (docs/proposals/beam-wave-3-handler-dispatch.md): shadow table
-- for core.agents — the second of the two coupled tables written on PATH-3
-- fresh mint (surface D in §3.1). Same design as 043_identities_shadow.sql;
-- see that migration's header for the FK and sequence rationale.
--
-- FK decision (§8.1): no foreign keys on the shadow table, by design —
-- write-only audit replica, not a referential target. (core.agents.id is
-- TEXT with no default, so there is no sequence-sharing concern here.)

CREATE TABLE IF NOT EXISTS core.agents_shadow (
    LIKE core.agents INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE core.agents_shadow IS
    'Wave 3 §8 shadow replica of core.agents. Write-only audit target for '
    'the BEAM shadow writer during the shadow window; no FKs by design; '
    'compared hourly by scripts/ops/wave-3-shadow-divergence-check.sql.';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (44, 'agents_shadow', NOW())
ON CONFLICT (version) DO NOTHING;
