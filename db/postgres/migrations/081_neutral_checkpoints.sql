-- Neutral capture is atomic with its sequence/head and durable event.
-- Assessment execution receipts are deliberately separate from immutable facts.
BEGIN;

CREATE TABLE IF NOT EXISTS core.checkpoint_streams (
    identity_id BIGINT PRIMARY KEY REFERENCES core.identities(identity_id),
    digest_salt BYTEA NOT NULL CHECK (octet_length(digest_salt) = 32),
    last_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    last_checkpoint_id UUID,
    CHECK ((last_seq = 0 AND last_checkpoint_id IS NULL)
        OR (last_seq > 0 AND last_checkpoint_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS core.checkpoints (
    checkpoint_id UUID PRIMARY KEY,
    identity_id BIGINT NOT NULL REFERENCES core.checkpoint_streams(identity_id),
    seq BIGINT NOT NULL CHECK (seq > 0),
    prev_checkpoint_id UUID,
    prev_seq BIGINT GENERATED ALWAYS AS
        (CASE WHEN seq = 1 THEN NULL ELSE seq - 1 END) STORED,
    idempotency_key_digest TEXT NOT NULL CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    request_digest TEXT NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    digest_version INTEGER NOT NULL DEFAULT 1 CHECK (digest_version = 1),
    accepted_attempt_id UUID NOT NULL UNIQUE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    report_excerpt TEXT CHECK (report_excerpt IS NULL OR char_length(report_excerpt) <= 2000),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    UNIQUE (identity_id, idempotency_key_digest),
    UNIQUE (identity_id, seq),
    UNIQUE (identity_id, checkpoint_id, seq),
    CHECK ((seq = 1 AND prev_checkpoint_id IS NULL)
        OR (seq > 1 AND prev_checkpoint_id IS NOT NULL)),
    FOREIGN KEY (identity_id, prev_checkpoint_id, prev_seq)
        REFERENCES core.checkpoints(identity_id, checkpoint_id, seq)
        DEFERRABLE INITIALLY IMMEDIATE
);

-- The cursor cannot refer to another identity or a different sequence.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'core.checkpoint_streams'::regclass
          AND conname = 'checkpoint_streams_head_fk') THEN
        ALTER TABLE core.checkpoint_streams ADD CONSTRAINT checkpoint_streams_head_fk
            FOREIGN KEY (identity_id, last_checkpoint_id, last_seq)
            REFERENCES core.checkpoints(identity_id, checkpoint_id, seq)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS core.checkpoint_outbox (
    event_id UUID PRIMARY KEY REFERENCES core.checkpoints(checkpoint_id),
    event_schema TEXT NOT NULL DEFAULT 'checkpoint.recorded.v1'
        CHECK (event_schema = 'checkpoint.recorded.v1'),
    identity_id BIGINT NOT NULL,
    seq BIGINT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (identity_id, event_id, seq)
        REFERENCES core.checkpoints(identity_id, checkpoint_id, seq),
    UNIQUE (identity_id, seq)
);

CREATE OR REPLACE FUNCTION core.reject_checkpoint_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'checkpoint records and events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS checkpoint_immutable ON core.checkpoints;
CREATE TRIGGER checkpoint_immutable BEFORE UPDATE ON core.checkpoints
    FOR EACH ROW EXECUTE FUNCTION core.reject_checkpoint_update();
DROP TRIGGER IF EXISTS checkpoint_outbox_immutable ON core.checkpoint_outbox;
CREATE TRIGGER checkpoint_outbox_immutable BEFORE UPDATE ON core.checkpoint_outbox
    FOR EACH ROW EXECUTE FUNCTION core.reject_checkpoint_update();

CREATE SCHEMA IF NOT EXISTS assessment;
CREATE TABLE IF NOT EXISTS assessment.checkpoint_deliveries (
    checkpoint_id UUID NOT NULL REFERENCES core.checkpoint_outbox(event_id),
    assessor TEXT NOT NULL CHECK (assessor ~ '^[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,79}$'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'started', 'completed', 'failed')),
    claim_token UUID,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (checkpoint_id, assessor),
    CHECK ((status = 'pending' AND claim_token IS NULL AND started_at IS NULL AND finished_at IS NULL)
        OR (status = 'started' AND claim_token IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NULL)
        OR (status IN ('completed', 'failed') AND claim_token IS NOT NULL
            AND started_at IS NOT NULL AND finished_at IS NOT NULL))
);

COMMENT ON TABLE core.checkpoints IS
    'Immutable neutral accepted occurrences. No report beyond scrubbed excerpt, raw key, request bytes, or assessor state.';
COMMENT ON TABLE core.checkpoint_streams IS
    'Transactionally serialized per-identity checkpoint head and stable digest salt; never an EISV counter.';
COMMENT ON TABLE core.checkpoint_outbox IS
    'Immutable neutral checkpoint.recorded.v1 events committed with acceptance. Zero subscribers are valid.';
COMMENT ON TABLE assessment.checkpoint_deliveries IS
    'Adapter execution claims: started is crash-ambiguous and never automatically retried; completed is execution return, not atomic assessment durability.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (81, 'neutral_checkpoints', now())
ON CONFLICT (version) DO NOTHING;
COMMIT;
