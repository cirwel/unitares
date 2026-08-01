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
--   4. Retention: the drop_old_*_partitions() functions compare partition
--      upper bounds as TIMESTAMPTZ instants instead of re-parsing a DATE out
--      of session-rendered text. This removes the last `current_date` in the
--      partition code. Detail and measurements under "RETENTION PATH" below.
--
-- WHY UTC, AND WHY NOW:
--   * 045's own header already named this as the intended end state
--     ("operator-gated detach/reattach normalization to uniform UTC bounds").
--     The Denver pin was never the destination; it was continuity scaffolding
--     for the live 2026-06/07 partitions that already had Denver bounds.
--   * UTC has no DST, so month edges stop moving an hour in absolute time
--     twice a year.
--   * THE STRONGEST ARGUMENT, stated plainly: a partition scheme keyed to
--     where a human happens to live is a latent defect, and the operator does
--     not intend to remain in Mountain Time. Had the host timezone changed
--     while the bounds stayed Denver-pinned, the two halves would have
--     disagreed IN PRODUCTION — the same failure CI hit, but against live
--     audit data. 055 removes that exposure permanently by making both halves
--     session-independent, instead of re-arming it at the next move.
--
-- WHAT THIS MIGRATION DOES *NOT* CHANGE, and a correction to the record:
--   Production Postgres runs `timezone = 'America/Denver'`
--   (postgresql.conf) and REMAINS so after 055. Only the partition BOUNDS
--   move to UTC. An earlier draft of this header — and the commit message,
--   which cannot be rewritten — claimed "CI already runs UTC; aligning
--   production to CI removes the environment divergence that produced this
--   incident." That is wrong twice over and the correct version belongs in
--   the permanent record:
--     (a) The live database was never exposed to the gap. Its Denver bounds
--         and its Denver `current_date` AGREED. Measured on live while
--         writing this: audit.partition_gaps() returns 0 rows, with 21
--         partitions across the three parents this incident broke
--         (events 8, tool_usage 5, outcome_events 8) and 28 across all five
--         partitioned audit parents. THE INCIDENT WAS CI-ONLY. 055 is a
--         latent-defect fix, not an outage repair.
--     (b) Production is not being "aligned to CI" — the session zone stays
--         Denver. What changes is that neither half depends on it any more.
--         The surviving Denver session is precisely why the RETENTION half
--         had to be de-sessioned in this same migration (see below); leaving
--         it reading session-rendered text would have left the bug class
--         alive in the one place the fix had not reached.
--
-- RETENTION PATH — the last session-rendered-text dependence, fixed here too.
--
--   audit.drop_old_events_partitions() / _tool_usage_ / _outcome_ decided what
--   to drop by regexing a DATE out of pg_get_expr(c.relpartbound, c.oid) and
--   comparing it against bare `current_date`. pg_get_expr renders a
--   timestamptz IN THE SESSION TimeZone. That is the SAME bug class as the
--   creation half this migration exists to fix — session-rendered text feeding
--   a bound computation — and it was the last `current_date` in the partition
--   code. On the live Denver-session host a partition bounded
--   TO ('2026-06-01 00:00:00+00') renders as '2026-05-31 18:00:00-06' and
--   parses back as 2026-05-31, one calendar day before its real end.
--
--   THE OBVIOUS READING OF THAT IS WRONG, so it is recorded measured rather
--   than argued. "The parsed date is a day early, therefore partitions drop a
--   day early and audit rows are silently deleted" does NOT follow:
--   `current_date` is rendered in the same session zone and shifts with it.
--   The two shifts largely cancel and the residual has a fixed sign.
--
--   MEASURED (scratch DB, 168 partition bounds covering every hour of seven
--   days chosen to span both US DST transitions, x retention N = 1..400 days,
--   x five session zones = 336,000 drop decisions, each compared against a
--   session-independent timestamptz reference):
--
--     session TimeZone           early drops (data loss)   late (extra retention)
--     UTC                                              0                       14
--     America/Denver                                   0                      137
--     Asia/Tokyo                                       0                       77
--     Pacific/Kiritimati (+14)                         0                      112
--     Pacific/Midway (-11)                             0                      105
--
--   ZERO early drops anywhere. The DATE-reparse predicate is provably weaker
--   than the correct one — floor_tz(bound) < floor_tz(now) - N implies
--   bound < now - N, because flooring is monotone — so it can only ever RETAIN
--   a partition the correct predicate would drop, never the reverse. Worst
--   observed error: 19.74 hours of EXTRA retention (Denver), bounded above by
--   |UTC offset| + 24h. No audit row was ever at risk of early deletion.
--
--   Nor did the UTC bound pin introduce it. Same sweep, Denver session, split
--   by bound convention: pre-055 Denver-midnight ends (06:00Z/07:00Z) gave 11
--   late divergences in 5,600 decisions; post-055 UTC-midnight ends gave 7 in
--   2,800. Both zero early. The defect predates 055 under both conventions;
--   055 removes it rather than creating it.
--
--   What it DOES do is make retention depend on where the session is pinned.
--   Measured end-to-end on one scratch database at one instant, on a
--   filler-shaped partition [2026-06-01 00:00Z, 12:00Z) that was genuinely
--   past a 60-day cutoff:
--       UTC session            -> dropped     (correct)
--       Asia/Tokyo session     -> dropped     (correct)
--       America/Denver session -> RETAINED    (its current_date was a day
--                                              behind, so the cutoff was too)
--   Three sessions, one database, one instant, two different answers. That is
--   the defect, and it is the same class as the original bug even though its
--   consequence is milder than the creation-half hole.
--
--   So this is a DETERMINISM fix, not a data-loss fix — and it is still worth
--   making. Hosts in different zones must not disagree about what "180 days"
--   means; retention should be the window that was configured; and leaving one
--   `current_date` behind in the file whose entire subject is session
--   dependence is an invitation to reintroduce the rest.
--
--   WHY `now()` AND AN HOURS INTERVAL, SPECIFICALLY. The natural-looking
--   rewrite is a trap, and was measured on the same 67,200-decision sweep:
--
--     bound < timezone('UTC', now()) - make_interval(days => N)
--         timezone('UTC', now()) returns TIMESTAMP WITHOUT TIME ZONE. Compared
--         against a timestamptz, PostgreSQL reinterprets it IN THE SESSION
--         ZONE, so in a Denver session the cutoff lands six hours in the
--         FUTURE. 46 decisions dropped a partition EARLY — e.g. a bound of
--         2026-06-01 04:00Z at N=61 dropped while the true cutoff was
--         2026-06-01 01:45Z, with 2h15m of retention still owed. This form
--         would have MANUFACTURED the data-loss bug that does not exist today.
--
--     bound < now() - make_interval(days => N)
--         now() is timestamptz so there is no reinterpretation — but
--         PostgreSQL does day arithmetic on timestamptz in the session zone,
--         so the cutoff moves an hour across a DST transition. 3 of 67,200
--         decisions dropped early in a Denver session.
--
--     bound < now() - make_interval(hours => N * 24)        <-- what 055 uses
--         Exact 24-hour days from an absolute instant. Identical decisions in
--         all five session zones; zero divergence.
--
--   The quoted-literal regex ('TO \(''([^'']+)''') replaces the date-shaped one
--   and still excludes DEFAULT and MAXVALUE partitions, whose bound text has no
--   quoted literal in that position.
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
-- 3. Retention — compare bounds as instants, never as re-parsed DATEs
--
--    The three function bodies below are byte-identical to the copies in
--    db/postgres/partitions.sql (the fresh-install bootstrap CI runs). Keep
--    them in sync.
-- ---------------------------------------------------------------------------

-- Retention cutoffs are compared as TIMESTAMPTZ, never as a re-parsed DATE
-- (migration 055). pg_get_expr() renders a timestamptz in the *session*
-- TimeZone, so slicing a DATE out of that text makes the drop decision depend
-- on where the caller's session is pinned — the same session-rendered-text
-- dependence that migration 045/055 removed from the creation half. See the
-- rationale block in 055 for the measured behaviour and why `now()` (not
-- `timezone('UTC', now())`) and an hours interval (not a days interval) are
-- the only combination that is genuinely session-independent.

-- Drop old event partitions (older than retention_days)
CREATE OR REPLACE FUNCTION audit.drop_old_events_partitions(
    p_retention_days INTEGER DEFAULT 180
)
RETURNS TABLE(partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff TIMESTAMPTZ;
    v_rec RECORD;
BEGIN
    -- Absolute instant, identical in every session TimeZone. now() is
    -- timestamptz; an hours interval avoids the calendar-day arithmetic that
    -- PostgreSQL performs in the session zone (and that therefore shifts by an
    -- hour across a DST transition).
    v_cutoff := now() - make_interval(hours => p_retention_days * 24);

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
        -- Extract the upper bound from the partition bound expression
        -- (e.g. "FOR VALUES FROM ('2025-01-01 00:00:00+00') TO ('2025-02-01 00:00:00+00')")
        -- and compare it as an instant. The quoted-literal regex also keeps
        -- DEFAULT and MAXVALUE partitions out of the retention path, exactly as
        -- the older date-shaped regex did.
        IF v_rec.partition_bound ~ 'TO \(''([^'']+)''' THEN
            DECLARE
                v_end TIMESTAMPTZ;
            BEGIN
                v_end := ((regexp_match(v_rec.partition_bound, 'TO \(''([^'']+)'''))[1])::TIMESTAMPTZ;
                IF v_end < v_cutoff THEN
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
    v_cutoff TIMESTAMPTZ;
    v_rec RECORD;
BEGIN
    -- Absolute instant, identical in every session TimeZone (see
    -- drop_old_events_partitions).
    v_cutoff := now() - make_interval(hours => p_retention_days * 24);

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
        IF v_rec.partition_bound ~ 'TO \(''([^'']+)''' THEN
            DECLARE
                v_end TIMESTAMPTZ;
            BEGIN
                v_end := ((regexp_match(v_rec.partition_bound, 'TO \(''([^'']+)'''))[1])::TIMESTAMPTZ;
                IF v_end < v_cutoff THEN
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
    v_cutoff TIMESTAMPTZ;
    v_rec RECORD;
BEGIN
    -- Absolute instant, identical in every session TimeZone (see
    -- drop_old_events_partitions).
    v_cutoff := now() - make_interval(hours => p_retention_days * 24);

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
        IF v_rec.partition_bound ~ 'TO \(''([^'']+)''' THEN
            DECLARE
                v_end TIMESTAMPTZ;
            BEGIN
                v_end := ((regexp_match(v_rec.partition_bound, 'TO \(''([^'']+)'''))[1])::TIMESTAMPTZ;
                IF v_end < v_cutoff THEN
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

-- audit.drop_old_r1_score_audit_partitions() carries the identical defect and
-- is repaired here too, so that no session-rendered-text retention predicate
-- survives this migration anywhere in the audit schema.
--
-- It is currently ORPHANED: migration 031 defined it AND called it from its
-- version of partition_maintenance(), but 045/055 replaced that function and
-- deliberately do not call it (r1_score_audit keeps full score history by
-- design — see the create-side note in section 2). Orphaned is not the same as
-- harmless: the function is still defined, still grantable, and still does
-- exactly what its name says if an operator or a future maintenance revision
-- calls it. Fixing it costs one CREATE OR REPLACE.
--
-- Not byte-shared with partitions.sql: that bootstrap file has no copy of this
-- function (031 owns it), so it sits outside the shared block above.
CREATE OR REPLACE FUNCTION audit.drop_old_r1_score_audit_partitions(
    p_retention_days INTEGER DEFAULT 180
)
RETURNS TABLE (partition_name TEXT, action TEXT) AS $$
DECLARE
    v_cutoff TIMESTAMPTZ;
    v_rec    RECORD;
BEGIN
    -- Absolute instant, identical in every session TimeZone (see
    -- drop_old_events_partitions).
    v_cutoff := now() - make_interval(hours => p_retention_days * 24);

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
        IF v_rec.partition_bound ~ 'TO \(''([^'']+)''' THEN
            DECLARE
                v_end TIMESTAMPTZ;
            BEGIN
                v_end := ((regexp_match(v_rec.partition_bound, 'TO \(''([^'']+)'''))[1])::TIMESTAMPTZ;
                IF v_end < v_cutoff THEN
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

-- ---------------------------------------------------------------------------
-- 4. Apply the normalization now (guarded for fresh installs where the audit
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
--   3. Do NOT reach for audit.partition_gaps() as the counter-argument — it
--      would not have caught this, and that limitation is the thing actually
--      worth recording here. Measured during the live failure,
--      partition_gaps() returned ZERO rows. It only finds holes BETWEEN
--      existing partitions; this hole was BEFORE the earliest one, so it is
--      structurally invisible to that query, DEFAULT partitions or not. The
--      detector read healthy straight through a partition-hole outage.
--      (It has a second blind spot documented in its own COMMENT: DEFAULT
--      partitions do not match the bound regex, so gaps adjacent to one are
--      invisible too.) So "it would blind the detector" is not the reason to
--      decline — but neither is "partition_gaps() is empty" ever evidence
--      that there is no hole. Treat it as weak evidence only, and if it is
--      ever wired to an alarm, extend it to cover the region before the
--      first partition and after the last.
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
