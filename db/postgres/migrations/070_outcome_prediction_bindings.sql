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
    'Canonical persisted provenance retained only for the bounded outcome idempotency window.';

CREATE OR REPLACE FUNCTION audit.cleanup_outcome_prediction_bindings(
    p_retention_days INTEGER DEFAULT 365
)
RETURNS BIGINT AS $$
DECLARE
    v_deleted BIGINT;
BEGIN
    IF p_retention_days < 0 THEN
        RAISE EXCEPTION 'retention days must be non-negative';
    END IF;

    DELETE FROM audit.outcome_prediction_bindings binding
    WHERE binding.canonical_outcome_ts
              < now() - make_interval(hours => p_retention_days * 24)
      AND NOT EXISTS (
          SELECT 1
          FROM audit.outcome_events outcome
          WHERE outcome.outcome_id = binding.canonical_outcome_id
      );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION audit.cleanup_outcome_prediction_bindings(INTEGER) IS
    'Deletes expired prediction claims only after their canonical outcomes have been retired.';

-- Migration 055 owns the current partition-drop implementation. Replace it
-- here so deployed databases clean the unpartitioned ledger before dropping
-- any canonical outcome partition. db/postgres/partitions.sql carries the same
-- definition for fresh bootstrap and re-runnable test schema setup.
CREATE OR REPLACE FUNCTION audit.drop_old_outcome_partitions(
    p_retention_days INTEGER DEFAULT 365
)
RETURNS TABLE(partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff TIMESTAMPTZ;
    v_rec RECORD;
BEGIN
    IF p_retention_days < 0 THEN
        RAISE EXCEPTION 'retention days must be non-negative';
    END IF;

    v_cutoff := now() - make_interval(hours => p_retention_days * 24);

    FOR v_rec IN
        SELECT c.relname AS partition_name,
               pg_get_expr(c.relpartbound, c.oid) AS partition_bound
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = i.inhparent
        WHERE n.nspname = 'audit'
          AND parent.relname = 'outcome_events'
          AND c.relkind = 'r'
    LOOP
        IF v_rec.partition_bound ~ 'TO \(''([^'']+)''' THEN
            DECLARE
                v_end TIMESTAMPTZ;
            BEGIN
                v_end := ((regexp_match(
                    v_rec.partition_bound,
                    'TO \(''([^'']+)'''
                ))[1])::TIMESTAMPTZ;
                IF v_end < v_cutoff THEN
                    EXECUTE format(
                        'DROP TABLE IF EXISTS audit.%I',
                        v_rec.partition_name
                    );
                    partition_name := v_rec.partition_name;
                    action := 'dropped';
                    RETURN NEXT;
                END IF;
            END;
        END IF;
    END LOOP;

    -- Partition drops and claim cleanup commit together. The cleanup helper
    -- refuses to remove a claim while its canonical outcome remains live.
    PERFORM audit.cleanup_outcome_prediction_bindings(p_retention_days);
END;
$$ LANGUAGE plpgsql;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (70, 'outcome_prediction_bindings', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
