-- 073_retention_keep_record.sql
--
-- audit.partition_maintenance() applied retention on every call: it dropped
-- audit.events months older than 180 days, audit.tool_usage months older
-- than 90, outcome_events months older than 365, and deleted every
-- core.agent_state row older than 90 days except each identity's latest
-- (core.cleanup_old_agent_state, added 2026-03-06). It runs weekly and also
-- on demand when an outcome insert finds its partition missing.
--
-- On a deployment running since November 2025 that left check-ins from
-- 2026-07-08 onward only; the earlier check-in history survives solely in
-- old database dumps, and part of it (about April to June 2026) in none.
-- The check-ins and outcomes are the record UNITARES exists to keep, and
-- analyses read them as history (the operator decided, 2026-10-07, to keep
-- them).
--
-- This redefines partition_maintenance() without any retention step. The
-- body is otherwise 055's, unchanged. Nothing is deleted or backfilled here.
-- audit.events and audit.tool_usage are the volume (about 12 GB live on the
-- reference deployment); their old months are exported to compressed files
-- and only then dropped, by scripts/ops/archive-audit-partitions.py.
-- core.cleanup_old_agent_state and the drop_old_* functions are kept for
-- that script and for deliberate manual use.
--
-- Everything is one transaction including registration, per 060.

BEGIN;

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

    -- No retention here (migration 073). This function also runs on demand
    -- whenever an outcome insert finds its partition missing, so it must not
    -- delete anything. Check-ins (core.agent_state) and outcome_events are
    -- kept; old audit.events and audit.tool_usage months are exported and
    -- then dropped by scripts/ops/archive-audit-partitions.py. The drop and
    -- cleanup functions remain for that script and for manual use.

    -- Clean up expired sessions
    v_result := v_result || jsonb_build_object(
        'sessions_cleaned',
        core.cleanup_expired_sessions()
    );

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION audit.partition_maintenance() IS
    'Fills detected gaps and ensures previous/current/next month partitions '
    'exist for the monthly-partitioned audit parents. Deletes nothing except '
    'expired sessions (migration 073); audit.events and audit.tool_usage '
    'retention is archive-then-drop in scripts/ops/archive-audit-partitions.py. '
    'Month selection is pinned to UTC (migration 055) so it agrees with the '
    'UTC month bounds regardless of the session TimeZone; bare current_date '
    'must never be reintroduced here.';

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (73, 'retention_keep_record', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
