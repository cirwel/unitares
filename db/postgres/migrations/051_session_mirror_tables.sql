-- 051_session_mirror_tables.sql
--
-- Redis-retirement Phase 1A (docs/proposals/redis-retirement-phase-1-plan.md):
-- durable PostgreSQL mirror for session/identity state that today lives only in
-- Redis. ADDITIVE and INERT: nothing writes here yet. The dual-write wiring
-- (modifying _cache_session / set_onboard_pin / resolution.py) is a separate,
-- flag-gated PR; this migration only lands the tables the DB methods target.
--
-- Two tables:
--   * core.onboard_pins      — durable mirror of Redis recent_onboard:* keys.
--     The IP:UA-fallback session-routing anchor (Claude Desktop, REST clients
--     without client_session_id). Phase 1A — needed regardless.
--   * core.session_bindings  — FK-less mirror of the Redis session: payload.
--     Phase 1B — built as instrumented shadow; kept only if a soak measurement
--     shows material cold-mints not covered by an onboard pin.
--
-- Design notes (from the v1.1 council review):
--   * NO foreign keys (cf. the 043/044 shadow replicas). The Redis session:
--     payload stores an agent_uuid *string* with no core.identities row for the
--     ephemeral persist=False majority; a FK would force eager identity
--     persistence, polluting the agent population. These are write-only
--     resolution mirrors, not referential targets.
--   * agent_uuid carries a CHECK enforcing UUID shape, so the "this column holds
--     the UUID-proof, never a display label" identity invariant is enforced at
--     the schema layer, not just by discipline.
--   * Column set verified against 865 live Redis session: payloads — includes
--     public_agent_id (59.8% of payloads) and api_key_hash (40.2%, legacy
--     sessions) which an earlier draft omitted.
--   * IF NOT EXISTS throughout so the migration is safe to re-run.
--   * Reaping: get_session_binding / lookup_onboard_pin_pg filter expires_at, so
--     expired rows are already invisible. Physical cleanup (extending
--     core.cleanup_expired_sessions) lands with the dual-write wiring PR, when
--     there is actually something to reap.

CREATE TABLE IF NOT EXISTS core.onboard_pins (
    fingerprint        TEXT PRIMARY KEY,   -- full suffix after "recent_onboard:" e.g. "ua:<hash>|<transport>|<model>" (1-3 pipe segments)
    agent_uuid         TEXT NOT NULL CHECK (agent_uuid ~ '^[0-9a-fA-F-]{36}$'),
    client_session_id  TEXT NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL   -- 30-min TTL (PIN_TTL=1800) in the Redis original
);

CREATE INDEX IF NOT EXISTS idx_onboard_pins_expires ON core.onboard_pins(expires_at);

CREATE TABLE IF NOT EXISTS core.session_bindings (
    session_key         TEXT PRIMARY KEY,
    agent_uuid          TEXT NOT NULL CHECK (agent_uuid ~ '^[0-9a-fA-F-]{36}$'),  -- maps from Redis JSON "agent_id"
    public_agent_id     TEXT NULL,
    display_agent_id    TEXT NULL,
    api_key_hash        TEXT NULL,
    spawn_reason        TEXT NULL,
    bind_ip_ua          TEXT NULL,   -- consumed by the PATH 2 fingerprint hijack check
    trajectory_required BOOLEAN NOT NULL DEFAULT FALSE,
    bind_count          INTEGER NOT NULL DEFAULT 1,
    bound_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NULL   -- NULL = permanent (Redis TTL=-1)
);

CREATE INDEX IF NOT EXISTS idx_session_bindings_expires ON core.session_bindings(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_bindings_uuid    ON core.session_bindings(agent_uuid);

COMMENT ON TABLE core.onboard_pins IS
    'Redis-retirement Phase 1A: durable mirror of recent_onboard:* — the IP:UA-'
    'fallback session-routing anchor. Inert until the dual-write wiring PR.';

COMMENT ON TABLE core.session_bindings IS
    'Redis-retirement Phase 1B: FK-less mirror of the Redis session: payload '
    '(session_key -> agent_uuid + rich fields). Inert; kept only if a shadow '
    'soak shows material cold-mints not covered by an onboard pin. Distinct from '
    'coordination.session_resolution_sagas (Wave 3 saga state).';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (51, 'session_mirror_tables', NOW())
ON CONFLICT (version) DO NOTHING;
