-- Pin-TTL Masking Hypothesis — audit.events analysis
--
-- Investigates whether the 30-minute sliding onboard pin TTL (_PIN_TTL=1800 in
-- src/mcp_handlers/identity/session.py) silently lets clients fall through to
-- continuity-token-only resumes between minutes 30-60 of a session.
--
-- Context: PR #203 shipped identity_resolution_observed instrumentation. One
-- event fires per onboard/resume call. The payload JSONB carries:
--   resolution_source       -- winning path (continuity_token, pinned_onboard_session, …)
--   pin_match_scope         -- client_model | client | model | unscoped | NULL
--   pin_entry_present       -- bool: did shadow pin lookup find an entry?
--   pin_fingerprint_match   -- bool: did that entry point at the resolved identity?
--   pin_entry_age_seconds   -- int: _PIN_TTL - TTL_remaining (age since last refresh)
--   token_iat / token_exp / token_age_seconds  -- continuity token metadata when present
--
-- Masking signal: continuity_token wins where pin_entry_present=true AND
-- pin_fingerprint_match=true mean a pin would have resolved the same agent but
-- token ordering shadowed it. Concentrated in the 0-1500s age band = bleed.
--
-- Usage via psql:
--   psql $GOVERNANCE_DATABASE_URL \
--     -v start_ts="'2026-04-26 00:00:00+00'" \
--     -v end_ts="'2026-05-03 23:59:59+00'" \
--     -f scripts/analysis/pin_ttl_bleed_analysis.sql
--
-- Usage via Python runner (preferred):
--   python3 scripts/analysis/pin_ttl_bleed_report.py [--days 7]
--
-- asyncpg placeholder form used by the Python runner: $1 = start_ts, $2 = end_ts

-- =============================================================================
-- Q1: Total event count
-- =============================================================================
SELECT
    count(*)::int AS total_events,
    min(ts)       AS window_start,
    max(ts)       AS window_end
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz;

-- =============================================================================
-- Q2: Resolution source distribution
-- =============================================================================
SELECT
    coalesce(payload->>'resolution_source', '(null)') AS resolution_source,
    count(*)::int                                       AS n
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz
GROUP BY 1
ORDER BY 2 DESC;

-- =============================================================================
-- Q3: Shadow-pin breakdown by resolution_source (all non-pin-path winners)
-- =============================================================================
SELECT
    coalesce(payload->>'resolution_source', '(null)')  AS resolution_source,
    count(*)::int                                        AS n,
    -- shadow ran and found an entry
    count(*) FILTER (
        WHERE (payload->>'pin_entry_present')::boolean IS TRUE
    )::int AS pin_present,
    -- pin entry present AND fingerprint matched (masking candidate)
    count(*) FILTER (
        WHERE (payload->>'pin_entry_present')::boolean IS TRUE
          AND (payload->>'pin_fingerprint_match')::boolean IS TRUE
    )::int AS present_and_match,
    -- pin entry present but fingerprint changed (legitimate divergence)
    count(*) FILTER (
        WHERE (payload->>'pin_entry_present')::boolean IS TRUE
          AND (payload->>'pin_fingerprint_match')::boolean IS FALSE
    )::int AS present_but_mismatch,
    -- shadow ran, no entry found (pin expired or never set)
    count(*) FILTER (
        WHERE (payload->>'pin_entry_present')::boolean IS FALSE
    )::int AS pin_absent,
    -- shadow did not run (NULL field)
    count(*) FILTER (
        WHERE payload->>'pin_entry_present' IS NULL
    )::int AS shadow_not_run
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz
  AND coalesce(payload->>'resolution_source', '') != 'pinned_onboard_session'
GROUP BY 1
ORDER BY 2 DESC;

-- =============================================================================
-- Q4: Pin-age-bucketed match rate — continuity_token wins where shadow ran
--
-- Buckets mirror quarter-TTL boundaries (1800s / 4 = 450s each):
--   0–449s   → pin very fresh (should always win if ordering matched)
--   450–899s → pin moderately aged
--   900–1499s→ pin approaching half-life
--   1500–1800s→ pin in final quarter, expiry imminent
--
-- High match rate in 0-1499s band with token winning = bleed evidence.
-- =============================================================================
SELECT
    CASE
        WHEN (payload->>'pin_entry_age_seconds')::int < 450   THEN '0–449s'
        WHEN (payload->>'pin_entry_age_seconds')::int < 900   THEN '450–899s'
        WHEN (payload->>'pin_entry_age_seconds')::int < 1500  THEN '900–1499s'
        WHEN (payload->>'pin_entry_age_seconds')::int <= 1800 THEN '1500–1800s'
        ELSE                                                        '>1800s (stale?)'
    END AS age_bucket,
    count(*)::int AS n,
    count(*) FILTER (
        WHERE (payload->>'pin_fingerprint_match')::boolean IS TRUE
    )::int AS pin_match,
    count(*) FILTER (
        WHERE (payload->>'pin_fingerprint_match')::boolean IS FALSE
    )::int AS pin_mismatch,
    round(
        100.0 * count(*) FILTER (
            WHERE (payload->>'pin_fingerprint_match')::boolean IS TRUE
        ) / nullif(count(*), 0),
        1
    ) AS match_pct
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz
  AND payload->>'resolution_source' = 'continuity_token'
  AND (payload->>'pin_entry_present')::boolean IS TRUE
GROUP BY 1
ORDER BY
    CASE
        WHEN (payload->>'pin_entry_age_seconds')::int < 450   THEN 1
        WHEN (payload->>'pin_entry_age_seconds')::int < 900   THEN 2
        WHEN (payload->>'pin_entry_age_seconds')::int < 1500  THEN 3
        WHEN (payload->>'pin_entry_age_seconds')::int <= 1800 THEN 4
        ELSE                                                        5
    END;

-- =============================================================================
-- Q5: Token-age × pin-state cross-tab (continuity_token wins with token present)
--
-- Shows whether token age (time since issue) correlates with pin-entry state.
-- If bleed is active, sessions with small token_age (fresh token, early in
-- session) should skew toward pin_present=true + pin_match=true because the
-- pin is still alive and would have resolved the same agent.
-- =============================================================================
SELECT
    CASE
        WHEN (payload->>'token_age_seconds')::int <  1800 THEN 'token<30m'
        WHEN (payload->>'token_age_seconds')::int <  3600 THEN 'token_30-60m'
        WHEN (payload->>'token_age_seconds')::int <  7200 THEN 'token_1h-2h'
        WHEN (payload->>'token_age_seconds')::int < 86400 THEN 'token_2h-24h'
        ELSE                                                    'token>24h'
    END                                        AS token_age_bucket,
    coalesce(payload->>'pin_entry_present', 'null')   AS pin_present,
    coalesce(payload->>'pin_fingerprint_match', 'null') AS pin_match,
    count(*)::int                              AS n
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz
  AND payload->>'resolution_source' = 'continuity_token'
  AND payload->>'token_age_seconds' IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- =============================================================================
-- Q6: Raw masking-candidate rows (top 20 by recency)
--
-- Rows where continuity_token won despite pin being present and matching.
-- The strongest direct evidence for the masking hypothesis.
-- =============================================================================
SELECT
    ts,
    payload->>'agent_id'               AS agent_id,
    payload->>'resolution_source'       AS resolution_source,
    payload->>'pin_match_scope'         AS pin_match_scope,
    (payload->>'pin_entry_age_seconds')::int AS pin_age_s,
    (payload->>'token_age_seconds')::int     AS token_age_s
FROM audit.events
WHERE event_type = 'identity_resolution_observed'
  AND ts >= $1::timestamptz
  AND ts <  $2::timestamptz
  AND payload->>'resolution_source' = 'continuity_token'
  AND (payload->>'pin_entry_present')::boolean IS TRUE
  AND (payload->>'pin_fingerprint_match')::boolean IS TRUE
ORDER BY ts DESC
LIMIT 20;
