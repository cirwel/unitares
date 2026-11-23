-- Durable deduplication ledger for POST /v1/external/events (GitHub outcome observer stage 1 part 2).
BEGIN;
CREATE TABLE IF NOT EXISTS audit.external_event_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    event_id    UUID NOT NULL,
    event_ts    TIMESTAMPTZ NOT NULL,
    producer    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_external_event_fingerprints_agent_ts
    ON audit.external_event_fingerprints (agent_id, event_ts DESC);
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (83, 'external_event_fingerprints', NOW()) ON CONFLICT (version) DO NOTHING;
COMMIT;
