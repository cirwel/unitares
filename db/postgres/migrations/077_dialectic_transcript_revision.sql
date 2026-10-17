-- 077_dialectic_transcript_revision.sql
--
-- Protocol messages and terminal decisions must agree on one durable
-- transcript frontier. A process-local transcript length cannot provide that
-- fence across Python workers or the BEAM terminal writer, so every accepted
-- protocol append increments this monotonic revision. Terminal writes compare
-- against the revision returned by the append they are resolving.

BEGIN;

ALTER TABLE core.dialectic_sessions
    ADD COLUMN IF NOT EXISTS transcript_revision BIGINT NOT NULL DEFAULT 0;

ALTER TABLE core.dialectic_sessions
    DROP CONSTRAINT IF EXISTS dialectic_sessions_transcript_revision_nonnegative;

ALTER TABLE core.dialectic_sessions
    ADD CONSTRAINT dialectic_sessions_transcript_revision_nonnegative
    CHECK (transcript_revision >= 0);

COMMENT ON COLUMN core.dialectic_sessions.transcript_revision IS
    'Monotonic protocol-message frontier. Guarded appends increment it; terminal writes compare the exact resulting revision.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (77, 'dialectic_transcript_revision', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
