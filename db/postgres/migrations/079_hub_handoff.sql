-- 079_hub_handoff.sql — MANUAL migration, do not auto-run.
--
-- Sign-in handoff for the local operator hub (portal-hub-v0 H2b). A signed-in
-- dashboard session approves the hub; the server issues a one-time code bound
-- to a PKCE challenge, and the hub exchanges it, over the server's owner-only
-- socket, for a hub session the server checks on every connect and write.
-- Only SHA-256 digests of codes and handles are stored. Apply explicitly
-- before setting UNITARES_HUB_CALLBACK_URL; the routes are inert until then.

BEGIN;

CREATE TABLE IF NOT EXISTS core.hub_auth_codes (
    code_hash BYTEA PRIMARY KEY,
    code_challenge TEXT NOT NULL,
    parent_session_hash BYTEA NOT NULL
        REFERENCES core.dashboard_sessions (session_hash) ON DELETE CASCADE,
    credential_id BYTEA NOT NULL
        REFERENCES core.webauthn_credentials (credential_id) ON DELETE CASCADE,
    operator_label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hub_auth_codes_expiry
    ON core.hub_auth_codes (expires_at);

CREATE TABLE IF NOT EXISTS core.hub_sessions (
    handle_hash BYTEA PRIMARY KEY,
    parent_session_hash BYTEA NOT NULL
        REFERENCES core.dashboard_sessions (session_hash) ON DELETE CASCADE,
    credential_id BYTEA NOT NULL
        REFERENCES core.webauthn_credentials (credential_id) ON DELETE CASCADE,
    operator_label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hub_sessions_expiry
    ON core.hub_sessions (expires_at)
    WHERE revoked_at IS NULL;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (79, 'hub_handoff', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
