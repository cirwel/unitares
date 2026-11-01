-- Durable work is created with the canonical outcome. Sink receipts are
-- committed with their state (PostgreSQL) or in the canonical state file.
BEGIN;

CREATE TABLE IF NOT EXISTS core.calibration_writer_receipts (
    writer_id UUID PRIMARY KEY,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    request_digest TEXT NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit.outcome_calibration_deliveries (
    outcome_id UUID PRIMARY KEY,
    outcome_ts TIMESTAMPTZ NOT NULL,
    agent_id TEXT NOT NULL,
    prediction_id TEXT NOT NULL,
    target_scope TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    needs_sequential BOOLEAN NOT NULL,
    calibration_delivered_at TIMESTAMPTZ,
    sequential_delivered_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, prediction_id),
    CHECK (completed_at IS NULL OR
           (calibration_delivered_at IS NOT NULL AND
            (NOT needs_sequential OR sequential_delivered_at IS NOT NULL)))
);

CREATE INDEX IF NOT EXISTS outcome_calibration_delivery_pending
ON audit.outcome_calibration_deliveries (target_scope, next_attempt_at, outcome_ts)
WHERE completed_at IS NULL;

COMMENT ON TABLE audit.outcome_calibration_deliveries IS
    'Frozen calibration work for newly created canonical outcomes; pending work survives outcome retention.';
COMMENT ON TABLE core.calibration_writer_receipts IS
    'One latest idempotency receipt per calibration writer; stale whole-state replacements require a version.';

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
          SELECT 1 FROM audit.outcome_events outcome
          WHERE outcome.ts = binding.canonical_outcome_ts
            AND outcome.outcome_id = binding.canonical_outcome_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM audit.outcome_calibration_deliveries delivery
          WHERE delivery.outcome_id = binding.canonical_outcome_id
            AND delivery.completed_at IS NULL
      );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    DELETE FROM audit.outcome_calibration_deliveries delivery
    WHERE delivery.completed_at IS NOT NULL
      AND delivery.outcome_ts < now() - make_interval(hours => p_retention_days * 24)
      AND NOT EXISTS (
          SELECT 1 FROM audit.outcome_prediction_bindings binding
          WHERE binding.canonical_outcome_id = delivery.outcome_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM audit.outcome_events outcome
          WHERE outcome.ts = delivery.outcome_ts
            AND outcome.outcome_id = delivery.outcome_id
      );
    RETURN v_deleted;
END;
$$ LANGUAGE plpgsql;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (80, 'outcome_calibration_delivery', now())
ON CONFLICT (version) DO NOTHING;
COMMIT;
