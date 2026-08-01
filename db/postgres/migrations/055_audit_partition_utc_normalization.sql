-- 055_audit_partition_utc_normalization.sql
--
-- Incident (2026-07-31): CI job `lease_plane` went red on every branch,
-- deterministically, with
--
--     ERROR 23514 check_violation: no partition of relation "events" found for row
--     Partition key of the failing row contains (ts) = (2026-08-01 00:22:52.207601+00)
--
-- and the same for audit.tool_usage — 16 failures, caused by no application
-- diff. The offending value is just now(). The CI runner is UTC and the clock
-- had just crossed into 2026-08-01Z.
--
-- ROOT CAUSE — the two halves of partition management disagreed on what a
-- "month" is:
--
--   * BOUNDS: audit.month_partition_bounds() pinned month edges to
--     America/Denver midnight (migration 045), i.e. August began at
--     2026-08-01 06:00Z.
--   * MONTH SELECTION: audit.partition_maintenance() (and partitions.sql's
--     bootstrap DO block) chose WHICH months to create from bare
--     `current_date`, which is evaluated in the *session* TimeZone.
--
-- On a UTC host during [00:00Z, 06:00Z) on the 1st, `current_date` already
-- reads the new month, so maintenance creates current=August and next=
-- September and never creates July — but under the Denver pin August does not
-- begin until 06:00Z. Those first six hours belong to no partition and every
-- insert in them hard-fails. Verified:
--
--     SET TIME ZONE 'UTC';            SELECT current_date; --> 2026-08-01
--     SET TIME ZONE 'America/Denver'; SELECT current_date; --> 2026-07-31
--     SELECT make_timestamptz(2026,8,1,0,0,0,'America/Denver');
--         --> 2026-08-01 00:00:00-06  (= 2026-08-01 06:00:00Z)
--
-- This is the SAME session-state-dependent-DDL bug class that migration 045
-- was written to repair. 045 fixed the bounds half (replacing DATE casts with
-- make_timestamptz + an explicit zone) and left the month-selection half
-- reading session-dependent `current_date`. Determinism on one side of a
-- boundary computation is not determinism.
--
-- THE FIX — normalize BOTH halves to UTC.
--
--   1. Bounds: America/Denver -> UTC in audit.month_partition_bounds().
--      The neighbour-snapping added by 045 is kept EXACTLY as-is; it is what
--      makes the convention change safe (see "why no detach/reattach" below).
--   2. Month selection: derive year/month from `timezone('UTC', now())`,
--      never from bare `current_date`. After this a host in ANY session
--      TimeZone computes the same set of months.
--   3. Also create the PREVIOUS month, not just current + next.
--
-- WHY UTC, AND WHY NOW:
--   * 045's own header already named this as the intended end state
--     ("operator-gated detach/reattach normalization to uniform UTC bounds").
--     The Denver pin was never the destination; it was continuity scaffolding
--     for the live 2026-06/07 partitions that already had Denver bounds.
--   * A partition scheme keyed to where a human happens to live is a latent
--     defect. The operator does not intend to remain in Mountain Time
--     indefinitely; UTC removes the coupling permanently.
--   * UTC has no DST, so month edges stop moving an hour in absolute time
--     twice a year.
--   * CI already runs UTC. Aligning production to CI removes the environment
--     divergence that produced this incident in the first place.
--
-- WHY NO DETACH/REATTACH IS NEEDED (this is the load-bearing claim):
--   045 added neighbour-snapping to month_partition_bounds, and its COMMENT
--   states the guarantee plainly — creation is "gapless and overlap-free
--   regardless of what convention older partitions used". That machinery
--   exists precisely to absorb a convention change at the seam. Concretely,
--   on the live DB (newest partition 2026_08 ending 2026-09-01 06:00Z):
--     - September's naive UTC lower bound (2026-09-01 00:00Z) SNAPS UP to
--       August's real end (2026-09-01 06:00Z), yielding one transitional
--       partition that is six hours short. No overlap, no gap.
--     - October computes clean UTC bounds [2026-10-01 00:00Z, 2026-11-01
--       00:00Z) and snapping is a no-op. Every month after it is clean UTC.
--     - Existing partitions are NEVER recreated: the create_* helpers
--       early-return on partition NAME existence, before bounds are computed.
--       Nothing already on disk is touched, moved, or rewritten.
--   So the cutover costs exactly one short partition and zero data movement.
--   A detach/reattach normalization would rewrite months of live audit data
--   to buy back six hours of alignment in a single already-closed month.
--
-- WHY ALSO CREATE THE PREVIOUS MONTH:
--   Cheap insurance against the mirror image of this bug. Month selection is
--   now UTC-deterministic in SQL, but the inputs above SQL are not: a host
--   whose clock skews backwards, a container started with a stale RTC, or a
--   maintenance run that fires seconds before a month rollover can all leave
--   the behind-us edge uncovered. On an established database creating the
--   previous month costs one name-existence check per parent and returns
--   "already exists"; it can only ever do work when something has genuinely
--   gone wrong. Retention (90d tool_usage / 180d events / 365d outcome, all
--   >= one month) guarantees the create-then-drop ordering inside
--   partition_maintenance() can never create a partition it then drops.
--
-- KNOWN RESIDUAL — audit.r1_score_audit on a NON-UTC fresh install.
--   Migration 031 creates r1_score_audit's first three partitions with its own
--   DATE-cast helper, and 031 runs BEFORE 045/055 replace that helper. So on a
--   fresh bootstrap those three months carry session-TimeZone bounds. Measured
--   on scratch databases bootstrapped at 2026-08-01T01:12Z:
--     UTC   session -> [2026-07-01 00:00Z, ...)   clean UTC
--     Denver session -> [2026-07-01 06:00Z, ...)  -06 offset
--     Tokyo  session -> [2026-07-01 00:00Z, 2026-07-31 15:00Z), ... +09 offset
--   In all three cases the chain is gapless and overlap-free
--   (audit.partition_gaps() empty), no insert of now() fails, and every month
--   created afterwards snaps back to clean UTC — verified out to 2027-01 on
--   both the Denver and Tokyo bootstraps. events / tool_usage / outcome_events
--   — the three parents this incident actually broke — are byte-identical
--   across all three session timezones after this change.
--   Not fixed here on purpose: correcting those three months would require
--   detaching and reattaching partitions that 031 already created by name, and
--   a wrong-bounds partition that silently absorbs a neighbour's rows is a far
--   worse failure than the six-hour offset it would repair. CI and the live
--   host are both effectively UTC, so nothing in the fleet is exposed. If 031's
--   bootstrap is ever revisited, that is the place to fix it.
--
-- DEFAULT PARTITIONS — DELIBERATELY NOT ADDED (see decision note at the foot
-- of this file).
--
-- db/postgres/partitions.sql (the fresh-install bootstrap) carries its own
-- copies of these functions and is updated in the same commit — CI bootstraps
-- a fresh database from it, so the bootstrap copy is what CI actually runs.
-- Keep the two in sync.

-- ---------------------------------------------------------------------------
-- 1. Month bounds — UTC pin, snapping unchanged
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

-- ---------------------------------------------------------------------------
-- 2. Maintenance — UTC month selection, plus the previous month
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 3. Apply the normalization now (guarded for fresh installs where the audit
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

-- ---------------------------------------------------------------------------
-- DECISION: no DEFAULT partitions for events / tool_usage / outcome_events /
-- r1_score_audit.
--
-- Considered and declined, deliberately, as part of this change.
--
-- The case FOR: audit.coordination_events has a DEFAULT partition and so
-- degrades gracefully instead of hard-failing when a month is missing. The
-- four parents above have none, which is why this incident surfaced as 16 red
-- CI tests rather than as nothing at all.
--
-- Why not, anyway:
--
--   1. It attacks the symptom, not the bug. The defect here was
--      non-deterministic DDL, now fixed at the source in both halves. A
--      DEFAULT would not have prevented the hole; it would have hidden it.
--   2. It makes future maintenance strictly harder. Attaching a concrete
--      partition whose range overlaps rows captured by a DEFAULT requires
--      detaching the DEFAULT first, and PostgreSQL full-scans it to validate
--      the new partition. That turns a routine monthly CREATE TABLE ...
--      PARTITION OF into a lock-heavy operation that can fail outright once
--      any row has landed in the default — exactly on the hot audit path.
--   3. It blinds the instrument that detects this bug class.
--      audit.partition_gaps()'s own COMMENT already documents the blind spot:
--      DEFAULT partitions do not match the bound regex, so gaps adjacent to
--      one become invisible and rows route to the DEFAULT instead of failing.
--      The detector for this failure mode would stop working.
--   4. The live evidence is against it. audit.coordination_events has no
--      rolling creation path — its newest concrete partition ends
--      2026-07-01, and 89 of its 106 rows (84%) now sit in
--      coordination_events_default. The "graceful degradation" quietly became
--      the primary storage path and nobody noticed for a month. That is the
--      documented failure pattern where instrumentation fails toward
--      "healthy" instead of toward "unknown".
--
-- A hard failure on a missing partition is loud, immediate, and points at the
-- exact defect. That is the behaviour worth keeping. If a softening is ever
-- wanted it belongs behind a real detector (partition_gaps() wired to an
-- alarm), not behind a silent catch-all — and it should be its own change
-- with its own evidence, not a rider on a determinism fix.
-- ---------------------------------------------------------------------------

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (55, 'audit_partition_utc_normalization', NOW())
ON CONFLICT (version) DO NOTHING;
