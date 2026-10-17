-- 075_dialectic_resolution_effects.sql
--
-- A terminal dialectic decision and the runtime effect it authorizes are two
-- different facts.  The decision is committed in PostgreSQL; applying its
-- conditions and resuming the paused agent happens in Python.  Record the
-- second fact in an outbox created by the same writer statement as the
-- terminal row so a lost HTTP response, task cancellation, or process crash
-- cannot strand an invisible committed decision.
--
-- This migration is deliberately PASSIVE.  Deploys apply migrations before
-- restarting the Python and BEAM writers, and database migrations are not
-- rolled back when application code rolls back.  A trigger installed here
-- would therefore enqueue ``pending`` work for an old writer that had already
-- applied the effect directly.  The compatible new writers use an
-- UPDATE+INSERT CTE; old writers ignore this additive table.

BEGIN;

CREATE SCHEMA IF NOT EXISTS coordination;

CREATE TABLE IF NOT EXISTS coordination.dialectic_resolution_effects (
    session_id                 TEXT PRIMARY KEY
                               REFERENCES core.dialectic_sessions(session_id)
                               ON DELETE CASCADE,
    paused_agent_id            TEXT NOT NULL,
    reviewer_agent_id          TEXT NOT NULL,
    effect_required            BOOLEAN NOT NULL,
    pause_token                TEXT,
    CHECK (
        (effect_required AND pause_token IS NOT NULL AND btrim(pause_token) <> '')
        OR (NOT effect_required AND pause_token IS NULL)
    ),
    resolution_payload_json    JSONB NOT NULL
                               CHECK (
                                   COALESCE(
                                       resolution_payload_json ?& ARRAY[
                                           'action', 'conditions', 'root_cause',
                                           'reasoning', 'signature_a', 'signature_b',
                                           'timestamp', 'signature_version'
                                       ]
                                       AND jsonb_typeof(resolution_payload_json) = 'object'
                                       AND resolution_payload_json->>'action' = 'resume'
                                       AND jsonb_typeof(resolution_payload_json->'conditions') = 'array'
                                       AND jsonb_typeof(resolution_payload_json->'root_cause') = 'string'
                                       AND jsonb_typeof(resolution_payload_json->'reasoning') = 'string'
                                       AND jsonb_typeof(resolution_payload_json->'signature_a') = 'string'
                                       AND jsonb_typeof(resolution_payload_json->'signature_b') = 'string'
                                       AND jsonb_typeof(resolution_payload_json->'timestamp') = 'string'
                                       AND jsonb_typeof(resolution_payload_json->'signature_version') = 'number',
                                       FALSE
                                   )
                               ),
    state                      TEXT NOT NULL DEFAULT 'pending'
                               CHECK (state IN ('pending', 'applying', 'applied', 'unknown_partial')),
    owner_token                TEXT,
    attempt_count              INTEGER NOT NULL DEFAULT 0
                               CHECK (attempt_count >= 0),
    last_error                 TEXT,
    execution_result_json      JSONB,
    claimed_at                 TIMESTAMPTZ,
    applied_at                 TIMESTAMPTZ,
    uncertain_at               TIMESTAMPTZ,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_dialectic_resolution_effects_actionable
    ON coordination.dialectic_resolution_effects (state, updated_at)
    WHERE state IN ('pending', 'applying', 'unknown_partial');

COMMENT ON TABLE coordination.dialectic_resolution_effects IS
    'Durable outbox for runtime effects authorized by resolved dialectic rows. '
    'Each compatible terminal row writer atomically inserts pending; one Python claimant '
    'moves pending -> applying -> applied, or records unknown_partial for manual reconciliation.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (75, 'dialectic_resolution_effects', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
