-- 078_dialectic_resolution_saga_revision.sql
--
-- Fence Python maintenance writes against a BEAM resolution claim that lands
-- after the maintenance worker's early saga check.  A successful BEAM claim
-- increments this value while holding the session row lock; every sweeper
-- compare-and-set binds the value it observed.  The claim therefore wins even
-- when it begins in the gap between the sweep read and its guarded UPDATE.

BEGIN;

ALTER TABLE core.dialectic_sessions
    ADD COLUMN IF NOT EXISTS resolution_saga_revision BIGINT NOT NULL DEFAULT 0;

ALTER TABLE core.dialectic_sessions
    DROP CONSTRAINT IF EXISTS dialectic_sessions_resolution_saga_revision_nonnegative;

ALTER TABLE core.dialectic_sessions
    ADD CONSTRAINT dialectic_sessions_resolution_saga_revision_nonnegative
    CHECK (resolution_saga_revision >= 0);

COMMENT ON COLUMN core.dialectic_sessions.resolution_saga_revision IS
    'Monotonic fence incremented by each successful BEAM resolution-saga claim; Python maintenance writes compare the observed value.';

ALTER TABLE coordination.session_resolution_sagas
    ADD COLUMN IF NOT EXISTS claim_revision BIGINT NOT NULL DEFAULT 0;

ALTER TABLE coordination.session_resolution_sagas
    ADD COLUMN IF NOT EXISTS claim_updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE coordination.session_resolution_sagas
SET claim_updated_at = COALESCE(claim_updated_at, created_at)
WHERE claim_updated_at IS NULL;

ALTER TABLE coordination.session_resolution_sagas
    ALTER COLUMN claim_updated_at SET NOT NULL;

ALTER TABLE coordination.session_resolution_sagas
    ALTER COLUMN claim_updated_at SET DEFAULT NOW();

ALTER TABLE coordination.session_resolution_sagas
    DROP CONSTRAINT IF EXISTS session_resolution_sagas_claim_revision_nonnegative;

ALTER TABLE coordination.session_resolution_sagas
    ADD CONSTRAINT session_resolution_sagas_claim_revision_nonnegative
    CHECK (claim_revision >= 0);

-- Idempotency belongs to the currently reserved generation.  The old
-- permanent (session,payload) key prevented a reopened session from making a
-- fresh fenced attempt with the same decision after a revert or earlier
-- commit.  The one-in-flight partial index remains the serialization gate.
ALTER TABLE coordination.session_resolution_sagas
    DROP CONSTRAINT IF EXISTS session_resolution_sagas_session_id_resolution_payload_hash_key;

CREATE INDEX IF NOT EXISTS idx_saga_session_payload
    ON coordination.session_resolution_sagas
       (session_id, resolution_payload_hash);

COMMENT ON COLUMN coordination.session_resolution_sagas.claim_revision IS
    'Session resolution_saga_revision owned by this claim generation; terminal commit requires the exact reserved saga and revision.';

COMMENT ON COLUMN coordination.session_resolution_sagas.claim_updated_at IS
    'Session updated_at immediately after reservation; terminal commit binds it so post-claim activity invalidates the decision.';

COMMENT ON COLUMN coordination.session_resolution_sagas.resolution_payload_hash IS
    'Legacy column name: SHA-256 of the canonical terminal-operation envelope (payload, requested status, and effect protocol), used for live-generation idempotency.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (78, 'dialectic_resolution_saga_revision', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
