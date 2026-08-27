-- 067_lease_attestation_nonces.sql
-- Durable single-use ledger for request-bound lease identity attestations.
--
-- A lat.v1 credential is signed, content-bound, and short-lived, but a bearer
-- could otherwise submit the same mutation twice before expiry.  The lease
-- plane atomically inserts (issuer, jti) before executing the mutation;
-- conflict means replay and is refused. Rows remain for a safety margin after
-- credential expiry so verifier/database clock skew cannot reopen replay.

BEGIN;

CREATE TABLE IF NOT EXISTS lease_plane.consumed_identity_attestations (
    issuer       TEXT        NOT NULL,
    jti          TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (issuer, jti)
);

CREATE INDEX IF NOT EXISTS idx_consumed_identity_attestations_expires_at
    ON lease_plane.consumed_identity_attestations (expires_at);

COMMENT ON TABLE lease_plane.consumed_identity_attestations IS
    'Single-use ledger for request-bound lat.v1 lease identity attestations. '
    'INSERT ... ON CONFLICT DO NOTHING closes replay before a lease mutation; '
    'rows may be purged only after expires_at plus the verifier safety margin.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (67, 'lease_attestation_nonces', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
