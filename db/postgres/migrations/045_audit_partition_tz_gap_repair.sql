-- 045_audit_partition_tz_gap_repair.sql
--
-- Incident (found 2026-06-11): audit partitions through 2026-05 carry
-- UTC-midnight bounds while 2026-06/07 carry America/Denver-midnight bounds,
-- leaving a six-hour hole [2026-05-31 18:00-06, 2026-06-01 00:00-06) in
-- audit.events, audit.tool_usage, and audit.outcome_events. Root cause: the
-- create-partition helpers computed bounds from DATE values, which cast to
-- timestamptz using the *session* TimeZone — the weekly maintenance task's
-- session default flipped from UTC to America/Denver between runs. Inserts
-- in the hole fail with "no partition of relation found for row"; the
-- lease-plane audit outbox forwarder (ORDER BY ts ASC LIMIT 100) head-of-line
-- blocked on 2,199 such rows and forwarded nothing after 2026-06-01.
--
-- This migration makes partition management timezone-deterministic and
-- self-healing:
--   1. audit.month_partition_bounds() — deterministic month edges pinned to
--      America/Denver (matching the live 2026-06/07 bounds so the chain
--      continues without overlap), snapped to neighboring partitions' bounds
--      so creation is gapless and overlap-free by construction.
--   2. audit.ensure_partition_indexes() — shared per-parent index DDL.
--   3. The three create_*_partition helpers rebuilt on (1)+(2).
--   4. audit.partition_gaps() — diagnostic: holes between consecutive bounds.
--   5. audit.partition_maintenance() — now fills any detected gap with a
--      bounds-exact filler partition (raising a WARNING for observability)
--      before the usual create/drop cycle.
--   6. Runs partition_maintenance() once, which repairs the live hole and
--      unblocks the forwarder backlog.
--
-- db/postgres/partitions.sql (the fresh-install bootstrap) is updated in the
-- same commit; keep the two in sync.

-- ---------------------------------------------------------------------------
-- 1. Deterministic, neighbor-snapped month bounds
-- ---------------------------------------------------------------------------

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
    -- Month edges at America/Denver midnight, independent of the session
    -- TimeZone. Denver (not UTC) because the live 2026-06/07 partitions
    -- already use Denver-midnight bounds — pinning UTC would overlap them
    -- at the next month boundary. make_timestamptz() with an explicit zone
    -- is immune to the session-TimeZone drift that caused the 2026-06 hole.
    -- NOTE: on a FRESH install the pin is the sole bound-determinant (no
    -- neighbor to snap to), so a future UTC-tz host bootstraps
    -- Denver-offset bounds by design — internally consistent and gapless,
    -- but offset from UTC month edges. Intended end-state is an
    -- operator-gated detach/reattach normalization to uniform UTC bounds;
    -- until then this pin and partitions.sql must stay in agreement.
    -- (Denver DST transitions fire at 02:00 local on Sundays; month-firsts
    -- at 00:00 are never skipped or ambiguous, verified 2000-2040.)
    v_start := make_timestamptz(p_year, p_month, 1, 0, 0, 0, 'America/Denver');
    IF p_month = 12 THEN
        v_end := make_timestamptz(p_year + 1, 1, 1, 0, 0, 0, 'America/Denver');
    ELSE
        v_end := make_timestamptz(p_year, p_month + 1, 1, 0, 0, 0, 'America/Denver');
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
    'Timezone-deterministic month partition bounds (America/Denver midnight), '
    'snapped to neighboring partition bounds so creation is gapless and '
    'overlap-free regardless of what convention older partitions used.';

-- ---------------------------------------------------------------------------
-- 2. Shared per-parent index DDL
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 3. Rebuild the three create helpers on (1) + (2)
-- ---------------------------------------------------------------------------

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

-- audit.r1_score_audit (migration 031) copied the same DATE-cast pattern and
-- — worse — had NO scheduled creation path at all: partitions existed only
-- through 2026-07, so R1 score persistence (record_r1_score_audit, a plain
-- INSERT that fails soft to False) would have silently stopped on 2026-08-01.
-- Rebuild it on the deterministic bounds and wire it into maintenance below.
-- No per-partition index DDL: r1's indexes are declared on the partitioned
-- parent (relkind 'I') and cascade to new partitions automatically.
CREATE OR REPLACE FUNCTION audit.create_r1_score_audit_partition(
    p_year  INTEGER,
    p_month INTEGER
)
RETURNS TEXT AS $$
DECLARE
    v_partition_name TEXT;
    v_bounds RECORD;
BEGIN
    v_partition_name := format('r1_score_audit_%s_%s', p_year, lpad(p_month::text, 2, '0'));

    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'audit' AND c.relname = v_partition_name
    ) THEN
        RETURN format('Partition %s already exists', v_partition_name);
    END IF;

    SELECT * INTO v_bounds
      FROM audit.month_partition_bounds('audit.r1_score_audit'::regclass, p_year, p_month);
    IF v_bounds.v_start >= v_bounds.v_end THEN
        RETURN format('Month %s-%s already covered by existing partitions',
                      p_year, lpad(p_month::text, 2, '0'));
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.r1_score_audit
         FOR VALUES FROM (%L) TO (%L)',
        v_partition_name, v_bounds.v_start, v_bounds.v_end
    );

    RETURN format('Created partition %s', v_partition_name);
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 4. Gap diagnostic
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 5. Maintenance now self-heals gaps
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit.partition_maintenance()
RETURNS JSONB AS $$
DECLARE
    v_result JSONB := '{}'::jsonb;
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

    -- Get current and next month
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

-- ---------------------------------------------------------------------------
-- 6. Repair the live hole now (guarded for fresh installs where the audit
--    tables may not exist yet when this file is applied out of order)
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('audit.events') IS NOT NULL
       AND to_regclass('audit.tool_usage') IS NOT NULL
       AND to_regclass('audit.outcome_events') IS NOT NULL THEN
        PERFORM audit.partition_maintenance();
    END IF;
END $$;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (45, 'audit_partition_tz_gap_repair', NOW())
ON CONFLICT (version) DO NOTHING;
