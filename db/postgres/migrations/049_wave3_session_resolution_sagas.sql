-- 049_wave3_session_resolution_sagas.sql
--
-- Wave 3 §9.1 (docs/proposals/beam-wave-3-handler-dispatch.md): crash-safe saga
-- state machine for dialectic session-resolution. Additive, zero cutover risk:
-- nothing writes here yet. The BEAM session GenServer (Wave 3 implementation —
-- NOT this migration) drives the saga forward path (§9.2) and crash recovery
-- (§9.3). This migration only lands the durable state table the executor will
-- need, so the executor PR is a code-only change against an existing schema.
--
-- DDL transcribed from RFC §9.1. Notes:
--   * FK to core.dialectic_sessions(session_id) is intentional here (unlike the
--     write-only shadow replicas in 043/044) — a saga is real coordination state
--     bound to a live session, so referential integrity is wanted.
--   * idx_saga_one_pending_per_session (partial unique) enforces the v0.3
--     code-reviewer invariant "at most one pending saga per session" at the DB
--     layer; a second-saga INSERT while one is pending fails the unique
--     constraint, which the session GenServer treats as a retryable conflict.
--   * IF NOT EXISTS throughout so the migration is safe to re-run.

CREATE SCHEMA IF NOT EXISTS coordination;

CREATE TABLE IF NOT EXISTS coordination.session_resolution_sagas (
    saga_id                    UUID PRIMARY KEY,
    session_id                 TEXT NOT NULL REFERENCES core.dialectic_sessions(session_id),
    paused_agent_id            TEXT NOT NULL,
    reviewer_agent_id          TEXT NOT NULL,
    state                      TEXT NOT NULL CHECK (state IN (
        'reserved',
        'paused_agent_applied',
        'both_agents_applied',
        'pg_committed',
        'reverting',
        'reverted'
    )),
    resolution_payload_json    JSONB NOT NULL,
    resolution_payload_hash    TEXT  NOT NULL,
    paused_agent_ack_at        TIMESTAMPTZ,
    reviewer_agent_ack_at      TIMESTAMPTZ,
    pg_committed_at            TIMESTAMPTZ,
    reverted_at                TIMESTAMPTZ,
    last_attempt_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count              INTEGER NOT NULL DEFAULT 0,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, resolution_payload_hash)
);

-- Prevent two pending sagas per session even when payload hashes differ.
CREATE UNIQUE INDEX IF NOT EXISTS idx_saga_one_pending_per_session
    ON coordination.session_resolution_sagas (session_id)
    WHERE state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting');

CREATE INDEX IF NOT EXISTS idx_saga_inflight
    ON coordination.session_resolution_sagas (state, last_attempt_at)
    WHERE state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting');

CREATE INDEX IF NOT EXISTS idx_saga_session
    ON coordination.session_resolution_sagas (session_id);

COMMENT ON TABLE coordination.session_resolution_sagas IS
    'Wave 3 §9 crash-safe saga state for dialectic session-resolution. Durable '
    'two-phase-apply log (reserved -> paused_agent_applied -> both_agents_applied '
    '-> pg_committed, with reverting/reverted compensation). Driven by the BEAM '
    'session GenServer; nothing writes here until the Wave 3 saga executor ships.';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (49, 'wave3_session_resolution_sagas', NOW())
ON CONFLICT (version) DO NOTHING;
