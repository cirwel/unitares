-- Partition Management for audit.events and audit.tool_usage
-- Run after schema.sql
--
-- Partition strategy: Monthly, with 180-day retention for events, 90-day for tool_usage
--
-- Usage:
--   1. Run this file to create initial partitions
--   2. Schedule partition_maintenance() weekly via pg_cron or external cron

-- =============================================================================
-- PARTITION CREATION FUNCTIONS
-- =============================================================================

-- Timezone-deterministic month bounds, snapped to neighbors (migration 045),
-- UTC-pinned (migration 055). This bootstrap file and migrations 045/055 carry
-- the same function bodies; keep them in sync.
CREATE OR REPLACE FUNCTION audit.month_partition_bounds(
    p_parent REGCLASS,
    p_year INTEGER,
    p_month INTEGER,
    OUT v_start TIMESTAMPTZ,
    OUT v_end TIMESTAMPTZ
) AS $$
DECLARE
    v_prev_end TIMESTAMPTZ;
    v_next_start TIMESTAMPTZ;
BEGIN
    -- Month edges at UTC midnight, independent of the session TimeZone.
    -- make_timestamptz() with an explicit zone is immune to the session-
    -- TimeZone drift that caused the 2026-06 hole; the zone is UTC (migration
    -- 055) rather than America/Denver (migration 045) so that partition
    -- boundaries do not depend on where the operator lives, and do not shift
    -- an hour in absolute time across DST transitions.
    --
    -- The Denver-bounded partitions that predate 055 are NOT rewritten. The
    -- neighbour-snapping below absorbs the convention change: the first month
    -- created after the cutover snaps its lower bound up to the last
    -- Denver-bounded partition's real end (one transitional partition, six
    -- hours short), and every month after that is clean UTC with snapping a
    -- no-op. Gapless and overlap-free by construction, so no detach/reattach
    -- normalization is required.
    v_start := make_timestamptz(p_year, p_month, 1, 0, 0, 0, 'UTC');
    IF p_month = 12 THEN
        v_end := make_timestamptz(p_year + 1, 1, 1, 0, 0, 0, 'UTC');
    ELSE
        v_end := make_timestamptz(p_year, p_month + 1, 1, 0, 0, 0, 'UTC');
    END IF;

    -- Snap the lower bound to the closest existing upper bound at or below
    -- v_end: extends downward over a hole, or shrinks upward past an
    -- existing partition that already covers the naive start.
    SELECT max(((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                              'TO \(''([^'']+)'''))[1])::timestamptz)
      INTO v_prev_end
      FROM pg_class c
      JOIN pg_inherits i ON i.inhrelid = c.oid
     WHERE i.inhparent = p_parent
       AND ((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                          'TO \(''([^'']+)'''))[1])::timestamptz <= v_end;
    IF v_prev_end IS NOT NULL AND v_prev_end <> v_start THEN
        v_start := v_prev_end;
    END IF;

    -- Snap the upper bound down to the next existing lower bound, if one
    -- starts inside our window (avoids overlap when backfilling).
    SELECT min(((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                              'FROM \(''([^'']+)'''))[1])::timestamptz)
      INTO v_next_start
      FROM pg_class c
      JOIN pg_inherits i ON i.inhrelid = c.oid
     WHERE i.inhparent = p_parent
       AND ((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                          'FROM \(''([^'']+)'''))[1])::timestamptz >= v_start;
    IF v_next_start IS NOT NULL AND v_next_start < v_end THEN
        v_end := v_next_start;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION audit.month_partition_bounds(REGCLASS, INTEGER, INTEGER) IS
    'Timezone-deterministic month partition bounds (UTC midnight, migration '
    '055; was America/Denver midnight in 045), snapped to neighboring '
    'partition bounds so creation is gapless and overlap-free regardless of '
    'what convention older partitions used.';

-- Shared per-parent index DDL (migration 045)
CREATE OR REPLACE FUNCTION audit.ensure_partition_indexes(
    p_parent TEXT,
    p_partition TEXT
) RETURNS VOID AS $$
BEGIN
    -- The three original audit parents predate partitioned indexes and carry
    -- per-partition index DDL. Parents whose indexes are declared ON the
    -- partitioned table (e.g. r1_score_audit, relkind 'I' indexes) cascade
    -- automatically and need nothing here.
    IF p_parent = 'events' THEN
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_agent_ts ON audit.%I (agent_id, ts DESC)',
            p_partition, p_partition
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_type_ts ON audit.%I (event_type, ts DESC)',
            p_partition, p_partition
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_hash ON audit.%I (raw_hash) WHERE raw_hash IS NOT NULL',
            p_partition, p_partition
        );
    ELSIF p_parent = 'tool_usage' THEN
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_agent_ts ON audit.%I (agent_id, ts DESC)',
            p_partition, p_partition
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_tool_ts ON audit.%I (tool_name, ts DESC)',
            p_partition, p_partition
        );
    ELSIF p_parent = 'outcome_events' THEN
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_agent_ts ON audit.%I (agent_id, ts DESC)',
            p_partition, p_partition
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_type_ts ON audit.%I (outcome_type, ts DESC)',
            p_partition, p_partition
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Create a monthly partition for audit.events
CREATE OR REPLACE FUNCTION audit.create_events_partition(
    p_year INTEGER,
    p_month INTEGER
)
RETURNS TEXT AS $$
DECLARE
    v_partition_name TEXT;
    v_bounds RECORD;
BEGIN
    v_partition_name := format('events_%s_%s', p_year, lpad(p_month::text, 2, '0'));

    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'audit' AND c.relname = v_partition_name
    ) THEN
        RETURN format('Partition %s already exists', v_partition_name);
    END IF;

    SELECT * INTO v_bounds
      FROM audit.month_partition_bounds('audit.events'::regclass, p_year, p_month);
    IF v_bounds.v_start >= v_bounds.v_end THEN
        RETURN format('Month %s-%s already covered by existing partitions',
                      p_year, lpad(p_month::text, 2, '0'));
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.events
         FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_bounds.v_start, v_bounds.v_end
    );
    PERFORM audit.ensure_partition_indexes('events', v_partition_name);

    RETURN format('Created partition %s', v_partition_name);
END;
$$ LANGUAGE plpgsql;

-- Create a monthly partition for audit.tool_usage
CREATE OR REPLACE FUNCTION audit.create_tool_usage_partition(
    p_year INTEGER,
    p_month INTEGER
)
RETURNS TEXT AS $$
DECLARE
    v_partition_name TEXT;
    v_bounds RECORD;
BEGIN
    v_partition_name := format('tool_usage_%s_%s', p_year, lpad(p_month::text, 2, '0'));

    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'audit' AND c.relname = v_partition_name
    ) THEN
        RETURN format('Partition %s already exists', v_partition_name);
    END IF;

    SELECT * INTO v_bounds
      FROM audit.month_partition_bounds('audit.tool_usage'::regclass, p_year, p_month);
    IF v_bounds.v_start >= v_bounds.v_end THEN
        RETURN format('Month %s-%s already covered by existing partitions',
                      p_year, lpad(p_month::text, 2, '0'));
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.tool_usage
         FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_bounds.v_start, v_bounds.v_end
    );
    PERFORM audit.ensure_partition_indexes('tool_usage', v_partition_name);

    RETURN format('Created partition %s', v_partition_name);
END;
$$ LANGUAGE plpgsql;

-- Create a monthly partition for audit.outcome_events
CREATE OR REPLACE FUNCTION audit.create_outcome_partition(
    p_year INTEGER,
    p_month INTEGER
)
RETURNS TEXT AS $$
DECLARE
    v_partition_name TEXT;
    v_bounds RECORD;
BEGIN
    v_partition_name := format('outcome_events_%s_%s', p_year, lpad(p_month::text, 2, '0'));

    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'audit' AND c.relname = v_partition_name
    ) THEN
        RETURN format('Partition %s already exists', v_partition_name);
    END IF;

    SELECT * INTO v_bounds
      FROM audit.month_partition_bounds('audit.outcome_events'::regclass, p_year, p_month);
    IF v_bounds.v_start >= v_bounds.v_end THEN
        RETURN format('Month %s-%s already covered by existing partitions',
                      p_year, lpad(p_month::text, 2, '0'));
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.outcome_events
         FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_bounds.v_start, v_bounds.v_end
    );
    PERFORM audit.ensure_partition_indexes('outcome_events', v_partition_name);

    RETURN format('Created partition %s', v_partition_name);
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- RETENTION / CLEANUP FUNCTIONS
-- =============================================================================

-- Drop old event partitions (older than retention_days)
CREATE OR REPLACE FUNCTION audit.drop_old_events_partitions(
    p_retention_days INTEGER DEFAULT 180
)
RETURNS TABLE(partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff DATE;
    v_rec RECORD;
BEGIN
    v_cutoff := current_date - (p_retention_days || ' days')::INTERVAL;

    FOR v_rec IN
        SELECT c.relname as partition_name,
               pg_get_expr(c.relpartbound, c.oid) as partition_bound
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = i.inhparent
        WHERE n.nspname = 'audit'
          AND parent.relname = 'events'
          AND c.relkind = 'r'
    LOOP
        -- Extract end date from partition bound (e.g., "FOR VALUES FROM ('2025-01-01') TO ('2025-02-01')")
        -- If end date < cutoff, drop it
        IF v_rec.partition_bound ~ 'TO \(''(\d{4}-\d{2}-\d{2})' THEN
            DECLARE
                v_end_date DATE;
            BEGIN
                v_end_date := (regexp_match(v_rec.partition_bound, 'TO \(''(\d{4}-\d{2}-\d{2})'))[1]::DATE;
                IF v_end_date < v_cutoff THEN
                    EXECUTE format('DROP TABLE IF EXISTS audit.%I', v_rec.partition_name);
                    partition_name := v_rec.partition_name;
                    action := 'dropped';
                    RETURN NEXT;
                END IF;
            END;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Drop old tool_usage partitions (older than retention_days)
CREATE OR REPLACE FUNCTION audit.drop_old_tool_usage_partitions(
    p_retention_days INTEGER DEFAULT 90
)
RETURNS TABLE(partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff DATE;
    v_rec RECORD;
BEGIN
    v_cutoff := current_date - (p_retention_days || ' days')::INTERVAL;

    FOR v_rec IN
        SELECT c.relname as partition_name,
               pg_get_expr(c.relpartbound, c.oid) as partition_bound
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = i.inhparent
        WHERE n.nspname = 'audit'
          AND parent.relname = 'tool_usage'
          AND c.relkind = 'r'
    LOOP
        IF v_rec.partition_bound ~ 'TO \(''(\d{4}-\d{2}-\d{2})' THEN
            DECLARE
                v_end_date DATE;
            BEGIN
                v_end_date := (regexp_match(v_rec.partition_bound, 'TO \(''(\d{4}-\d{2}-\d{2})'))[1]::DATE;
                IF v_end_date < v_cutoff THEN
                    EXECUTE format('DROP TABLE IF EXISTS audit.%I', v_rec.partition_name);
                    partition_name := v_rec.partition_name;
                    action := 'dropped';
                    RETURN NEXT;
                END IF;
            END;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Drop old outcome_events partitions (older than retention_days)
CREATE OR REPLACE FUNCTION audit.drop_old_outcome_partitions(
    p_retention_days INTEGER DEFAULT 365
)
RETURNS TABLE(partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff DATE;
    v_rec RECORD;
BEGIN
    v_cutoff := current_date - (p_retention_days || ' days')::INTERVAL;

    FOR v_rec IN
        SELECT c.relname as partition_name,
               pg_get_expr(c.relpartbound, c.oid) as partition_bound
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = i.inhparent
        WHERE n.nspname = 'audit'
          AND parent.relname = 'outcome_events'
          AND c.relkind = 'r'
    LOOP
        IF v_rec.partition_bound ~ 'TO \(''(\d{4}-\d{2}-\d{2})' THEN
            DECLARE
                v_end_date DATE;
            BEGIN
                v_end_date := (regexp_match(v_rec.partition_bound, 'TO \(''(\d{4}-\d{2}-\d{2})'))[1]::DATE;
                IF v_end_date < v_cutoff THEN
                    EXECUTE format('DROP TABLE IF EXISTS audit.%I', v_rec.partition_name);
                    partition_name := v_rec.partition_name;
                    action := 'dropped';
                    RETURN NEXT;
                END IF;
            END;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- MAINTENANCE FUNCTION (call weekly)
-- =============================================================================

CREATE OR REPLACE FUNCTION audit.partition_gaps()
RETURNS TABLE(parent TEXT, gap_start TIMESTAMPTZ, gap_end TIMESTAMPTZ) AS $$
    WITH bounds AS (
        SELECT parent.relname AS parent,
               ((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                              'FROM \(''([^'']+)'''))[1])::timestamptz AS lo,
               ((regexp_match(pg_get_expr(c.relpartbound, c.oid),
                              'TO \(''([^'']+)'''))[1])::timestamptz AS hi
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = parent.relnamespace
        WHERE n.nspname = 'audit'
          AND parent.relname IN ('events', 'tool_usage', 'outcome_events',
                                 'r1_score_audit')
          AND c.relkind = 'r'
    ), ordered AS (
        SELECT parent, lo, hi,
               lead(lo) OVER (PARTITION BY parent ORDER BY lo) AS next_lo
        FROM bounds
        WHERE lo IS NOT NULL AND hi IS NOT NULL
    )
    SELECT parent, hi, next_lo
    FROM ordered
    WHERE next_lo IS NOT NULL AND next_lo > hi;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION audit.partition_gaps() IS
    'Holes between consecutive partition bounds of the monthly-partitioned '
    'audit parents. Non-empty output means inserts in the hole fail with '
    '"no partition of relation found for row". Blind spot: DEFAULT and '
    'MINVALUE/MAXVALUE partitions do not match the bound regex and are '
    'excluded — if a DEFAULT partition is ever added to these parents, '
    'gaps adjacent to it become invisible here (rows route to the DEFAULT '
    'instead of failing).';


CREATE OR REPLACE FUNCTION audit.partition_maintenance()
RETURNS JSONB AS $$
DECLARE
    v_result JSONB := '{}'::jsonb;
    v_now_utc TIMESTAMP;
    v_prev_year INTEGER;
    v_prev_month INTEGER;
    v_current_year INTEGER;
    v_current_month INTEGER;
    v_next_year INTEGER;
    v_next_month INTEGER;
    v_msg TEXT;
    v_gap RECORD;
    v_fill_name TEXT;
    v_filled JSONB := '[]'::jsonb;
BEGIN
    -- Fill any holes between existing partition bounds first, so rows
    -- stranded in a hole (and retrying writers, e.g. the lease-plane audit
    -- outbox forwarder) recover without operator action.
    FOR v_gap IN SELECT * FROM audit.partition_gaps() LOOP
        v_fill_name := format('%s_fill_%s', v_gap.parent,
                              to_char(v_gap.gap_start AT TIME ZONE 'UTC',
                                      'YYYYMMDD_HH24MI'));
        -- An orphaned table squatting on the filler name would make
        -- CREATE TABLE IF NOT EXISTS silently skip while the gap stays
        -- open — surface that instead of warning identically every week.
        IF EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'audit' AND c.relname = v_fill_name
              AND NOT EXISTS (
                  SELECT 1 FROM pg_inherits i
                  JOIN pg_class p ON p.oid = i.inhparent
                  WHERE i.inhrelid = c.oid AND p.relname = v_gap.parent
              )
        ) THEN
            RAISE WARNING 'audit.% exists but is not attached to audit.%; '
                'gap [% - %) cannot be auto-filled — manual intervention required',
                v_fill_name, v_gap.parent, v_gap.gap_start, v_gap.gap_end;
            CONTINUE;
        END IF;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.%I
             FOR VALUES FROM (%L) TO (%L)',
            v_fill_name, v_gap.parent, v_gap.gap_start, v_gap.gap_end
        );
        PERFORM audit.ensure_partition_indexes(v_gap.parent, v_fill_name);
        v_filled := v_filled || jsonb_build_object(
            'partition', v_fill_name,
            'gap_start', v_gap.gap_start,
            'gap_end', v_gap.gap_end
        );
        RAISE WARNING 'audit partition gap filled: % covers [% - %)',
            v_fill_name, v_gap.gap_start, v_gap.gap_end;
    END LOOP;
    IF jsonb_array_length(v_filled) > 0 THEN
        v_result := v_result || jsonb_build_object('gaps_filled', v_filled);
    END IF;

    -- Month selection in UTC (migration 055). NEVER use bare `current_date`
    -- here: it is evaluated in the session TimeZone, so a UTC host and a
    -- Denver host disagree about which month it is for six hours after every
    -- month rollover. That disagreement against the UTC-pinned bounds above
    -- is what left [00:00Z, 06:00Z) on the 1st with no partition and turned
    -- every insert in that window into a check_violation.
    v_now_utc := timezone('UTC', now());

    v_prev_year     := EXTRACT(YEAR  FROM v_now_utc - INTERVAL '1 month')::INTEGER;
    v_prev_month    := EXTRACT(MONTH FROM v_now_utc - INTERVAL '1 month')::INTEGER;
    v_current_year  := EXTRACT(YEAR  FROM v_now_utc)::INTEGER;
    v_current_month := EXTRACT(MONTH FROM v_now_utc)::INTEGER;
    v_next_year     := EXTRACT(YEAR  FROM v_now_utc + INTERVAL '1 month')::INTEGER;
    v_next_month    := EXTRACT(MONTH FROM v_now_utc + INTERVAL '1 month')::INTEGER;

    -- Previous month (migration 055). Month selection is now deterministic in
    -- SQL, but the inputs above SQL are not — a skewed clock, a stale
    -- container RTC, or a run firing seconds before rollover can leave the
    -- behind-us edge uncovered. On an established database this is one
    -- name-existence check per parent returning 'already exists'; it can only
    -- do real work when something upstream has gone wrong. Safe against the
    -- create-then-drop ordering below because every retention window
    -- (90/180/365 days) exceeds one month.
    v_msg := audit.create_events_partition(v_prev_year, v_prev_month);
    v_result := v_result || jsonb_build_object('events_prev', v_msg);

    v_msg := audit.create_tool_usage_partition(v_prev_year, v_prev_month);
    v_result := v_result || jsonb_build_object('tool_usage_prev', v_msg);

    v_msg := audit.create_outcome_partition(v_prev_year, v_prev_month);
    v_result := v_result || jsonb_build_object('outcome_events_prev', v_msg);

    -- Ensure current month partitions exist
    v_msg := audit.create_events_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('events_current', v_msg);

    v_msg := audit.create_tool_usage_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('tool_usage_current', v_msg);

    v_msg := audit.create_outcome_partition(v_current_year, v_current_month);
    v_result := v_result || jsonb_build_object('outcome_events_current', v_msg);

    -- Create next month partitions (look-ahead)
    v_msg := audit.create_events_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('events_next', v_msg);

    v_msg := audit.create_tool_usage_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('tool_usage_next', v_msg);

    v_msg := audit.create_outcome_partition(v_next_year, v_next_month);
    v_result := v_result || jsonb_build_object('outcome_events_next', v_msg);

    -- r1_score_audit (migration 031) — guarded because the fresh-install
    -- bootstrap (partitions.sql) defines this maintenance function before
    -- migration 031 creates the r1 table. No retention drop by design: the
    -- audit table keeps full score history (public KG nodes are the
    -- 30-day-archived projection, see r1_maintenance.py).
    IF to_regclass('audit.r1_score_audit') IS NOT NULL
       AND to_regprocedure('audit.create_r1_score_audit_partition(integer, integer)') IS NOT NULL THEN
        v_msg := audit.create_r1_score_audit_partition(v_prev_year, v_prev_month);
        v_result := v_result || jsonb_build_object('r1_score_audit_prev', v_msg);

        v_msg := audit.create_r1_score_audit_partition(v_current_year, v_current_month);
        v_result := v_result || jsonb_build_object('r1_score_audit_current', v_msg);

        v_msg := audit.create_r1_score_audit_partition(v_next_year, v_next_month);
        v_result := v_result || jsonb_build_object('r1_score_audit_next', v_msg);
    END IF;

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

COMMENT ON FUNCTION audit.partition_maintenance() IS
    'Fills detected gaps, then ensures previous/current/next month partitions '
    'exist for the monthly-partitioned audit parents and applies retention. '
    'Month selection is pinned to UTC (migration 055) so it agrees with the '
    'UTC month bounds regardless of the session TimeZone; bare current_date '
    'must never be reintroduced here.';


-- =============================================================================
-- INITIAL PARTITION CREATION
-- Create partitions for the previous month, the current month, and the next 2
-- =============================================================================

DO $$
DECLARE
    v_now_utc TIMESTAMP;
    v_year INTEGER;
    v_month INTEGER;
    v_i INTEGER;
BEGIN
    -- UTC month selection (migration 055), matching the UTC-pinned bounds in
    -- audit.month_partition_bounds(). This block used bare `current_date`,
    -- which is evaluated in the session TimeZone: on a UTC host during
    -- [00:00Z, 06:00Z) on the 1st it already read the new month while the
    -- Denver-pinned bounds said the new month had not started, so a fresh
    -- bootstrap produced a database whose earliest partition began six hours
    -- in the future and every insert of now() hard-failed. That is the exact
    -- fresh-install path CI takes (docker-initdb.sh), and it is what turned
    -- the lease_plane job red on every branch on 2026-08-01.
    --
    -- Start at the previous month for the same reason partition_maintenance()
    -- does: it costs one no-op name check per parent on any established
    -- database and closes the behind-us edge if the host clock skews backwards.
    v_now_utc := timezone('UTC', now());

    FOR v_i IN -1..2 LOOP
        v_year := EXTRACT(YEAR FROM v_now_utc + (v_i || ' month')::INTERVAL)::INTEGER;
        v_month := EXTRACT(MONTH FROM v_now_utc + (v_i || ' month')::INTERVAL)::INTEGER;

        PERFORM audit.create_events_partition(v_year, v_month);
        PERFORM audit.create_tool_usage_partition(v_year, v_month);
        PERFORM audit.create_outcome_partition(v_year, v_month);
    END LOOP;
END $$;

-- =============================================================================
-- OPTIONAL: pg_cron SCHEDULING
-- Uncomment if pg_cron is installed
-- =============================================================================

-- SELECT cron.schedule(
--     'partition-maintenance',
--     '0 3 * * 0',  -- Every Sunday at 3 AM
--     'SELECT audit.partition_maintenance()'
-- );

-- =============================================================================
-- UTILITY VIEWS
-- =============================================================================

-- List all partitions with row counts and sizes
CREATE OR REPLACE VIEW audit.v_partition_stats AS
SELECT
    n.nspname as schema_name,
    parent.relname as parent_table,
    c.relname as partition_name,
    pg_get_expr(c.relpartbound, c.oid) as partition_bounds,
    pg_size_pretty(pg_relation_size(c.oid)) as size,
    (SELECT count(*) FROM pg_class WHERE oid = c.oid) as approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class parent ON parent.oid = i.inhparent
WHERE n.nspname = 'audit'
  AND c.relkind = 'r'
ORDER BY parent.relname, c.relname;

COMMENT ON VIEW audit.v_partition_stats IS 'Shows all audit partitions with sizes and bounds';
