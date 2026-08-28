-- 068_agent_orchestrator_idempotency.sql
--
-- Durable, bounded idempotency ledger for the BEAM agent orchestrator.
--
-- POST /v1/agents accepts an Idempotency-Key. The orchestrator stores only a
-- SHA-256 key hash, the already-canonical spawn-spec digest, a server-minted
-- execution id, and lifecycle timestamps. It deliberately does NOT persist
-- the raw key, command, arguments, environment, captured output, or secrets.
--
-- The reservation/started split is load-bearing. A row is reserved before the
-- OS process is opened and marked started afterwards. If the orchestrator dies
-- between those two non-transactional effects, the durable row remains
-- reserved and a retry fails closed as outcome-unknown instead of risking a
-- duplicate external process.

BEGIN;

CREATE SCHEMA IF NOT EXISTS orchestration;

CREATE TABLE IF NOT EXISTS orchestration.spawn_idempotency (
    key_hash       TEXT PRIMARY KEY
                   CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    spec_digest    TEXT NOT NULL
                   CHECK (spec_digest ~ '^[0-9a-f]{64}$'),
    execution_id  TEXT NOT NULL UNIQUE
                   CHECK (execution_id ~ '^ex-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    state         TEXT NOT NULL DEFAULT 'reserved'
                   CHECK (state IN ('reserved', 'started')),
    reserved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    CONSTRAINT spawn_idempotency_state_time_consistent CHECK (
        (state = 'reserved' AND started_at IS NULL)
        OR (state = 'started' AND started_at IS NOT NULL)
    ),
    CONSTRAINT spawn_idempotency_expiry_after_reservation CHECK (
        expires_at > reserved_at
    )
);

CREATE INDEX IF NOT EXISTS idx_spawn_idempotency_expires_at
    ON orchestration.spawn_idempotency (expires_at);

COMMENT ON TABLE orchestration.spawn_idempotency IS
    'Bounded restart-safe idempotency ledger for agent-orchestrator spawns. '
    'Contains hashes and execution metadata only; never spawn specs, env, '
    'secrets, or output. Reserved rows represent a crash-ambiguous interval '
    'and must fail closed rather than be respawned.';

COMMENT ON COLUMN orchestration.spawn_idempotency.key_hash IS
    'Lowercase SHA-256 of the caller Idempotency-Key; the raw key is not stored.';

COMMENT ON COLUMN orchestration.spawn_idempotency.spec_digest IS
    'Lowercase SHA-256 of the canonical material spawn spec; no spec fields are stored.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (68, 'agent_orchestrator_idempotency', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
