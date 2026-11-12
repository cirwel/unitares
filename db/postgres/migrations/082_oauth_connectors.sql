-- Operator-managed OAuth clients. Registration has no TTL; token state stays
-- in the existing OAuth store. No plaintext connector secrets are persisted.
BEGIN;
CREATE TABLE IF NOT EXISTS core.oauth_connectors (
    client_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    redirect_uris JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT
);
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (82, 'oauth_connectors', NOW()) ON CONFLICT (version) DO NOTHING;
COMMIT;
