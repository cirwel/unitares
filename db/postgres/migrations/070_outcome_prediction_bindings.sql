-- 070_outcome_prediction_bindings.sql
--
-- Durable exactly-once binding for prediction-bound outcome submissions.
--
-- audit.outcome_events is range-partitioned on ts, so prediction_id cannot be
-- made globally unique there without including the partition key. This
-- unpartitioned ledger owns the global (agent_id, prediction_id) claim and
-- retains the exact (ts, outcome_id) pair used to address the canonical row.
-- The application inserts the claim and partitioned outcome in one transaction.
--
-- A referential constraint is deliberately omitted: the partitioned parent's
-- key is (ts, outcome_id), while this ledger must be the authoritative global
-- uniqueness surface. Transactional creation plus the retained composite
-- address prevents an acknowledged ledger row without its canonical outcome.

BEGIN;

CREATE TABLE IF NOT EXISTS audit.outcome_prediction_bindings (
    agent_id                 TEXT NOT NULL,
    prediction_id            TEXT NOT NULL,
    request_digest           TEXT NOT NULL
                             CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    canonical_outcome_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    canonical_outcome_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    claim_token              UUID NOT NULL DEFAULT gen_random_uuid(),
    canonical_outcome_type   TEXT NOT NULL,
    canonical_outcome_score  REAL,
    canonical_is_bad         BOOLEAN NOT NULL,
    canonical_detail         JSONB NOT NULL,
    canonical_eisv_snapshot  JSONB,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, prediction_id),
    UNIQUE (claim_token),
    UNIQUE (canonical_outcome_ts, canonical_outcome_id)
);

-- Keep the migration re-runnable while this pre-release schema is exercised by
-- the shared test database. If an earlier review revision created the ledger,
-- recover canonical replay fields from the outcome row rather than inventing
-- provenance. An orphaned partial claim fails loudly below.
ALTER TABLE audit.outcome_prediction_bindings
    ADD COLUMN IF NOT EXISTS canonical_outcome_type TEXT,
    ADD COLUMN IF NOT EXISTS canonical_outcome_score REAL,
    ADD COLUMN IF NOT EXISTS canonical_is_bad BOOLEAN,
    ADD COLUMN IF NOT EXISTS canonical_detail JSONB;

UPDATE audit.outcome_prediction_bindings AS binding
SET canonical_outcome_type = outcome.outcome_type,
    canonical_outcome_score = outcome.outcome_score,
    canonical_is_bad = outcome.is_bad,
    canonical_detail = outcome.detail
FROM audit.outcome_events AS outcome
WHERE outcome.ts = binding.canonical_outcome_ts
  AND outcome.outcome_id = binding.canonical_outcome_id
  AND (binding.canonical_outcome_type IS NULL
       OR binding.canonical_is_bad IS NULL
       OR binding.canonical_detail IS NULL);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM audit.outcome_prediction_bindings
        WHERE canonical_outcome_type IS NULL
           OR canonical_is_bad IS NULL
           OR canonical_detail IS NULL
    ) THEN
        RAISE EXCEPTION
            'cannot backfill canonical replay fields for an existing outcome prediction binding';
    END IF;
END
$$;

ALTER TABLE audit.outcome_prediction_bindings
    ALTER COLUMN canonical_outcome_type SET NOT NULL,
    ALTER COLUMN canonical_is_bad SET NOT NULL,
    ALTER COLUMN canonical_detail SET NOT NULL;

COMMENT ON TABLE audit.outcome_prediction_bindings IS
    'Authoritative exactly-once ledger for prediction-bound outcome_event submissions. '
    'The claim and canonical partitioned outcome row are committed atomically.';

COMMENT ON COLUMN audit.outcome_prediction_bindings.request_digest IS
    'SHA-256 of canonical caller-controlled outcome semantics; excludes transient registry and EISV state.';

COMMENT ON COLUMN audit.outcome_prediction_bindings.canonical_eisv_snapshot IS
    'Full original response snapshot retained so identical retries survive process restart without provenance drift.';

COMMENT ON COLUMN audit.outcome_prediction_bindings.canonical_detail IS
    'Canonical persisted provenance retained beyond outcome partition retention for deterministic replay.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (70, 'outcome_prediction_bindings', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
