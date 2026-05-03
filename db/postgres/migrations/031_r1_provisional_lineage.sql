-- Migration 031: R1 provisional-lineage columns on core.identities + audit.r1_score_audit
--
-- Adds the SQL source-of-truth for the R1 v3.3 provisional-lineage lifecycle
-- and the audit-only persistence target for full per-score records. Public KG
-- carries only the redacted projection (verdict + calibration_status +
-- n_dims_used + score_id per v3.3-A); this audit table holds the rest.
--
-- See: docs/ontology/r1-verify-lineage-claim.md §v3.3-D (storage decision)
--      docs/ontology/r1-verify-lineage-claim.md §v3.3-E (table name + retention)

-- =============================================================================
-- core.identities — provisional-lineage columns
-- =============================================================================

ALTER TABLE core.identities
    ADD COLUMN IF NOT EXISTS provisional_lineage     BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS provisional_score_id    UUID        NULL,
    ADD COLUMN IF NOT EXISTS provisional_recorded_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS confirmed_at            TIMESTAMPTZ NULL;

COMMENT ON COLUMN core.identities.provisional_lineage     IS 'R1 v3.3-D: TRUE when most recent score returned inconclusive and call-site policy was marks';
COMMENT ON COLUMN core.identities.provisional_score_id    IS 'R1 v3.3-D: references audit.r1_score_audit.score_id of the score that justified the current state';
COMMENT ON COLUMN core.identities.provisional_recorded_at IS 'R1 v3.3-D: when the current provisional_lineage value was last set';
COMMENT ON COLUMN core.identities.confirmed_at            IS 'R1 v3.3-D: stamped on provisional → confirmed transition';

-- No new indexes at v0. Provisional rows expected to be a tiny fraction of
-- core.identities; index decisions deferred until a query motivates them.

-- =============================================================================
-- audit.r1_score_audit — full per-score record (audit-only persistence)
-- =============================================================================
--
-- Partitioning: monthly RANGE on recorded_at, mirroring audit.events.
-- Retention: 180 days (per v3.3-E operator decision; calibration analysis
-- needs long enough windows to diagnose separation failure, especially for
-- low-volume class partitions like subagent declared-lineage pairs).

CREATE TABLE IF NOT EXISTS audit.r1_score_audit (
    score_id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    parent_id           TEXT        NOT NULL,
    successor_id        TEXT        NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    plausibility        REAL        NOT NULL,
    components          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    observations        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    parent_mature       BOOLEAN     NOT NULL DEFAULT FALSE,
    reasons             TEXT[]      NOT NULL DEFAULT ARRAY[]::TEXT[],
    class_tag           TEXT        NULL,
    calibration_status  TEXT        NOT NULL DEFAULT 'seeded'
                        CHECK (calibration_status IN ('seeded', 'earned', 'calibration_failed')),
    PRIMARY KEY (score_id, recorded_at)
) PARTITION BY RANGE (recorded_at);

CREATE INDEX IF NOT EXISTS idx_r1_score_audit_pair_time
    ON audit.r1_score_audit (parent_id, successor_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_r1_score_audit_class_status
    ON audit.r1_score_audit (class_tag, calibration_status)
    WHERE class_tag IS NOT NULL;

COMMENT ON TABLE audit.r1_score_audit IS 'R1 v3.3-A: full per-score record (audit-only); public KG carries only the redacted projection joined via score_id';

-- =============================================================================
-- Partition management — mirrors audit.create_events_partition pattern
-- =============================================================================

CREATE OR REPLACE FUNCTION audit.create_r1_score_audit_partition(
    p_year  INTEGER,
    p_month INTEGER
)
RETURNS TEXT AS $$
DECLARE
    v_partition_name TEXT;
    v_start_date     DATE;
    v_end_date       DATE;
BEGIN
    v_partition_name := format('r1_score_audit_%s_%s', p_year, lpad(p_month::text, 2, '0'));
    v_start_date := make_date(p_year, p_month, 1);
    v_end_date   := v_start_date + INTERVAL '1 month';

    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'audit' AND c.relname = v_partition_name
    ) THEN
        RETURN format('Partition %s already exists', v_partition_name);
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.r1_score_audit
         FOR VALUES FROM (%L) TO (%L)',
        v_partition_name,
        v_start_date,
        v_end_date
    );

    RETURN format('Created partition %s', v_partition_name);
END;
$$ LANGUAGE plpgsql;

-- Mirrors audit.drop_old_events_partitions: read partition_bound via
-- pg_get_expr and regex-extract the end date. Matches existing function
-- shape (partition_name TEXT, action TEXT) for consistent jsonb_agg.
--
-- DROP first because Postgres CREATE OR REPLACE cannot change RETURNS
-- signature; defensive against any prior version of this function.
DROP FUNCTION IF EXISTS audit.drop_old_r1_score_audit_partitions(INTEGER);

CREATE OR REPLACE FUNCTION audit.drop_old_r1_score_audit_partitions(
    p_retention_days INTEGER DEFAULT 180
)
RETURNS TABLE (partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff DATE;
    v_rec    RECORD;
BEGIN
    v_cutoff := current_date - (p_retention_days || ' days')::INTERVAL;

    FOR v_rec IN
        SELECT c.relname AS pname,
               pg_get_expr(c.relpartbound, c.oid) AS partition_bound
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits inh ON inh.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = inh.inhparent
        WHERE n.nspname = 'audit'
          AND parent.relname = 'r1_score_audit'
          AND c.relkind = 'r'
    LOOP
        IF v_rec.partition_bound ~ 'TO \(''(\d{4}-\d{2}-\d{2})' THEN
            DECLARE
                v_end_date DATE;
            BEGIN
                v_end_date := (regexp_match(v_rec.partition_bound, 'TO \(''(\d{4}-\d{2}-\d{2})'))[1]::DATE;
                IF v_end_date < v_cutoff THEN
                    EXECUTE format('DROP TABLE IF EXISTS audit.%I', v_rec.pname);
                    partition_name := v_rec.pname;
                    action := 'dropped';
                    RETURN NEXT;
                END IF;
            END;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Initial partitions — current + next 2 months
-- =============================================================================

DO $$
DECLARE
    v_now DATE := CURRENT_DATE;
BEGIN
    PERFORM audit.create_r1_score_audit_partition(EXTRACT(YEAR  FROM v_now)::INTEGER,
                                                  EXTRACT(MONTH FROM v_now)::INTEGER);
    PERFORM audit.create_r1_score_audit_partition(EXTRACT(YEAR  FROM v_now + INTERVAL '1 month')::INTEGER,
                                                  EXTRACT(MONTH FROM v_now + INTERVAL '1 month')::INTEGER);
    PERFORM audit.create_r1_score_audit_partition(EXTRACT(YEAR  FROM v_now + INTERVAL '2 months')::INTEGER,
                                                  EXTRACT(MONTH FROM v_now + INTERVAL '2 months')::INTEGER);
END $$;

-- =============================================================================
-- audit.partition_maintenance — extend to cover r1_score_audit
-- =============================================================================
--
-- Mirrors the existing function body (db/postgres/partitions.sql:280) and
-- splices in r1_score_audit create_current/next + drop_old_180d. Keeping the
-- r1 lifecycle wired into the same maintenance entrypoint avoids a parallel
-- cron schedule and matches the events / tool_usage / outcome_events pattern.

CREATE OR REPLACE FUNCTION audit.partition_maintenance()
RETURNS JSONB AS $$
DECLARE
    v_result JSONB := '{}'::jsonb;
    v_current_year INTEGER;
    v_current_month INTEGER;
    v_next_year INTEGER;
    v_next_month INTEGER;
    v_msg TEXT;
BEGIN
    v_current_year := EXTRACT(YEAR FROM current_date)::INTEGER;
    v_current_month := EXTRACT(MONTH FROM current_date)::INTEGER;

    IF v_current_month = 12 THEN
        v_next_year := v_current_year + 1;
        v_next_month := 1;
    ELSE
        v_next_year := v_current_year;
        v_next_month := v_current_month + 1;
    END IF;

    -- Ensure current month partitions exist
    v_msg := audit.create_events_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('events_current', v_msg);

    v_msg := audit.create_tool_usage_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('tool_usage_current', v_msg);

    v_msg := audit.create_outcome_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('outcome_events_current', v_msg);

    v_msg := audit.create_r1_score_audit_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('r1_score_audit_current', v_msg);

    -- Create next month partitions (look-ahead)
    v_msg := audit.create_events_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('events_next', v_msg);

    v_msg := audit.create_tool_usage_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('tool_usage_next', v_msg);

    v_msg := audit.create_outcome_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('outcome_events_next', v_msg);

    v_msg := audit.create_r1_score_audit_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('r1_score_audit_next', v_msg);

    -- Clean up old partitions
    v_result := v_result || jsonb_build_object(
        'events_dropped',
        (SELECT jsonb_agg(partition_name) FROM audit.drop_old_events_partitions(180))
    );
    v_result := v_result || jsonb_build_object(
        'tool_usage_dropped',
        (SELECT jsonb_agg(partition_name) FROM audit.drop_old_tool_usage_partitions(90))
    );
    v_result := v_result || jsonb_build_object(
        'outcome_events_dropped',
        (SELECT jsonb_agg(partition_name) FROM audit.drop_old_outcome_partitions(365))
    );
    v_result := v_result || jsonb_build_object(
        'r1_score_audit_dropped',
        (SELECT jsonb_agg(partition_name) FROM audit.drop_old_r1_score_audit_partitions(180))
    );

    -- Clean up expired sessions
    v_result := v_result || jsonb_build_object(
        'sessions_cleaned',
        core.cleanup_expired_sessions()
    );

    -- Clean up old agent_state rows (keep last 90 days)
    v_result := v_result || jsonb_build_object(
        'agent_state_cleaned',
        core.cleanup_old_agent_state(90)
    );

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Register migration
-- =============================================================================

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (31, 'r1_provisional_lineage', NOW())
ON CONFLICT (version) DO NOTHING;
