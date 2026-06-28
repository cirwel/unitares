-- 052_effects_payloads.sql
-- Durable storage for the governed-effect EXECUTE half (Phase 4, §5b/§5/§10).
--
-- The first reversible execute surface (file_write) needs a place to hold the
-- actual bytes the runtime commits AND the pre-image needed for rollback. The
-- record_only path (#1065) stores nothing here — it rides audit.events. This
-- table exists only for execute custody: pre-image capture, crash-recovery
-- content-hash reconciliation, and tombstone/quarantine state.
--
-- MANUAL migration. Do NOT auto-run. Apply with psql before the new lease-plane
-- binary that references effects.* starts. Until applied, EffectRepo queries
-- error and the (flag-off) execute path stays execute_not_implemented.

CREATE SCHEMA IF NOT EXISTS effects;

CREATE TABLE IF NOT EXISTS effects.payloads (
    effect_id          TEXT PRIMARY KEY,
    effect_type        TEXT NOT NULL,
    -- the scrubbed bytes the executor applies (per-class ceiling enforced in
    -- Elixir before insert, never raw-logged). NULL until insert.
    payload_bytes      BYTEA,
    payload_sha256     TEXT NOT NULL,
    -- §5b rollback: the file's bytes/hash BEFORE the write, captured by the
    -- executor immediately before mutating. pre_image_existed=false means the
    -- file did not exist (rollback = delete what we created).
    pre_image_sha256   TEXT,
    pre_image_bytes    BYTEA,
    pre_image_existed  BOOLEAN NOT NULL DEFAULT FALSE,
    -- rollback lifecycle. NULL = fresh or cleanly committed.
    --   'pending'     — pre-image captured, mutation in flight (crash here →
    --                   recovery reconciles by content hash).
    --   'tombstoned'  — rolled back; a same-key retry MUST re-execute (§4/§5b).
    --   'quarantined' — apply failed AND compensation failed; surface is dirty,
    --                   operator-first, retry is unsafe.
    rollback_state     TEXT CHECK (rollback_state IN ('pending', 'tombstoned', 'quarantined')),
    committed_at       TIMESTAMPTZ,
    -- stored for crash-recovery re-acquire (the surface lease(s) to re-hold
    -- before any compensation).
    required_leases    JSONB NOT NULL DEFAULT '[]',
    proposer_agent_uuid TEXT,
    idempotency_key    TEXT NOT NULL,
    idempotency_digest TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Boot-time EffectRecovery scans only orphans: pre-image captured, never
-- committed. Partial index keeps that scan cheap.
CREATE INDEX IF NOT EXISTS idx_effects_payloads_orphans
    ON effects.payloads (created_at)
    WHERE rollback_state = 'pending' AND committed_at IS NULL;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (52, 'effects_payloads', NOW())
ON CONFLICT (version) DO NOTHING;
