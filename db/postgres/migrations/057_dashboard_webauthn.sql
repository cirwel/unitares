-- 057_dashboard_webauthn.sql — MANUAL migration, do not auto-run.
--
-- Browser passkey credentials and opaque, revocable dashboard sessions.
-- Apply explicitly before deploying the passkey server routes.

BEGIN;

CREATE TABLE IF NOT EXISTS core.webauthn_credentials (
    credential_id BYTEA PRIMARY KEY,
    public_key BYTEA NOT NULL,
    user_handle BYTEA NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    transports TEXT[],
    operator_label TEXT NOT NULL,
    nickname TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_webauthn_user_handle
    ON core.webauthn_credentials (user_handle)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS core.dashboard_sessions (
    session_hash BYTEA PRIMARY KEY,
    credential_id BYTEA NOT NULL
        REFERENCES core.webauthn_credentials (credential_id),
    operator_label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    hard_expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry
    ON core.dashboard_sessions (expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS core.webauthn_challenges (
    pre_session_hash BYTEA PRIMARY KEY,
    challenge BYTEA NOT NULL,
    ceremony TEXT NOT NULL
        CHECK (ceremony IN ('register', 'authenticate')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expiry
    ON core.webauthn_challenges (expires_at);

CREATE TABLE IF NOT EXISTS core.webauthn_enroll_codes (
    code_hash BYTEA PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (57, 'dashboard_webauthn', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
