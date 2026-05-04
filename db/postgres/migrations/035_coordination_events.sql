-- Migration 035: Wave 0 coordination_events instrumentation
--
-- Implements docs/proposals/beam-footprint-roadmap-v0.md Wave 0:
-- "emit structured events on coordination-class failures (asyncpg connect
--  errors, anyio task-group cancellations, executor pool exhaustion, MCP
--  handler timeouts) and persist them in a Chronicler-readable form.
--  Without Wave 0, no later wave's exit criterion can be honestly evaluated."
--
-- Single-table replay surface (NOT per-service tables) — every coordination-
-- class failure across sentinel/governance_mcp/lease_plane/vigil/chronicler/
-- watcher writes here. Wave 1's exit criterion (incident-rate trends) reads
-- from this single surface.
--
-- Envelope per the roadmap (locked upfront, not evolved):
--   event_id     UUID         primary identity / replay-dedup key
--   ts           timestamptz  ordering, audit, partition key
--   service      TEXT enum    originator (sentinel|governance_mcp|...)
--   event_type   TEXT dotted  coordination_failure.<class> + future namespaces
--   agent_id     TEXT NULL    UNITARES UUID when agent-attributable
--   payload      JSONB        event-type-specific structure (per-event_type docs)
--   context      JSONB        emitter facts: git_commit, service_pid,
--                             running_since, host
--
-- Stability discipline: event_type extends by adding new dotted namespaces,
-- never by reusing or renaming existing ones. payload shape per event_type
-- is documented at the time the event_type lands.

CREATE TABLE IF NOT EXISTS audit.coordination_events (
    ts          timestamptz NOT NULL,
    event_id    UUID        NOT NULL DEFAULT gen_random_uuid(),
    service     TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,
    agent_id    TEXT        NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    context     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ts, event_id)
) PARTITION BY RANGE (ts);

-- Service enum CHECK. Locked to the six emitters named in the roadmap
-- envelope spec. New services added explicitly (alter the CHECK; not
-- ad-hoc string append).
DO $$ BEGIN
    ALTER TABLE audit.coordination_events
        ADD CONSTRAINT coordination_events_service_check
        CHECK (service = ANY (ARRAY[
            'sentinel',
            'governance_mcp',
            'lease_plane',
            'vigil',
            'chronicler',
            'watcher'
        ]::text[]));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- event_type namespace CHECK. Wave 0 lands the coordination_failure.* family.
-- Future waves (e.g. coordination_recovery.*, coordination_lifecycle.*) extend
-- by adding the family prefix here, never by reusing existing values.
DO $$ BEGIN
    ALTER TABLE audit.coordination_events
        ADD CONSTRAINT coordination_events_event_type_namespace
        CHECK (event_type ~ '^(coordination_failure)\.[a-z_]+$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- payload + context MUST be JSON objects (not bare null/scalar/array).
-- Mirrors the v0.10 §7.2.8 contract pattern from substrate_state — readers
-- can rely on payload->>'<key>' and context->>'<key>' shape.
DO $$ BEGIN
    ALTER TABLE audit.coordination_events
        ADD CONSTRAINT coordination_events_payload_object
        CHECK (jsonb_typeof(payload) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE audit.coordination_events
        ADD CONSTRAINT coordination_events_context_object
        CHECK (jsonb_typeof(context) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Indexes for the standard read patterns: per-service incident timeline
-- (Sentinel rule polling), per-event-type counts (dashboard panel), and
-- per-agent attribution (when agent_id is set).
CREATE INDEX IF NOT EXISTS idx_coord_events_service_ts
    ON audit.coordination_events (service, ts DESC);

CREATE INDEX IF NOT EXISTS idx_coord_events_event_type_ts
    ON audit.coordination_events (event_type, ts DESC);

CREATE INDEX IF NOT EXISTS idx_coord_events_agent_ts
    ON audit.coordination_events (agent_id, ts DESC)
    WHERE agent_id IS NOT NULL;

-- Initial partitions: current month + next month + a default catch-all so
-- writes never fail on an unbounded ts. Future partitions are operator-
-- managed via the existing db/postgres/partitions.sql pattern (Vigil
-- maintains it on its 30min cadence).
CREATE TABLE IF NOT EXISTS audit.coordination_events_2026_05
    PARTITION OF audit.coordination_events
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

CREATE TABLE IF NOT EXISTS audit.coordination_events_2026_06
    PARTITION OF audit.coordination_events
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE TABLE IF NOT EXISTS audit.coordination_events_default
    PARTITION OF audit.coordination_events DEFAULT;

COMMENT ON TABLE audit.coordination_events IS 'Wave 0 (RFC docs/proposals/beam-footprint-roadmap-v0.md): single-surface replay log for coordination-class failures. Sentinel-style alarm rules + dashboard panel + Chronicler projection all read from here. event_type extends by adding new dotted namespaces; never reuse or rename existing values.';

COMMENT ON COLUMN audit.coordination_events.event_type IS 'Dotted namespace per roadmap §94. Wave 0 locks coordination_failure.<class>: asyncpg_connect_error, anyio_cancellation, executor_pool_exhaustion, mcp_handler_timeout. Future waves extend the family prefix (coordination_recovery.*, etc.).';

COMMENT ON COLUMN audit.coordination_events.context IS 'Facts about the emitter (git_commit, service_pid, running_since, host) — NOT facts about the event. Populated by the central emitter; callers do not pass this directly.';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (35, 'coordination_events', NOW())
ON CONFLICT (version) DO NOTHING;
