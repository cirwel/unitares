-- ODE-fallback reach — dated deployment snapshot, read-only
--
-- Query text behind the "Reach" paragraph of
-- docs/ontology/eisv-proprioception-contract.md, section "Fitted and learned
-- estimators — which bucket they sit in (2026-09-25)". Run once on 2026-09-25
-- for the window [2026-09-18T00:00Z, 2026-09-25T00:00Z).
--
-- Definitions:
--   row                 -- core.agent_state row with synthetic IS NOT TRUE and
--                          state_json.eisv_telemetry.schema = 'eisv.telemetry.v1'
--   behavioral-primary  -- a row whose persisted
--                          state_json.eisv_telemetry.measurement.primary.source
--                          is 'behavioral' (the other value is 'ode_fallback')
--   fallback-only       -- an identity with rows in the window and no
--                          behavioral-primary row among them
--
-- Recorded result (2026-09-25): 276 identities, 7,789 rows (387 ode_fallback,
-- 7,402 behavioral); 172 fallback-only identities, all with at most 2 rows in
-- the window (147 with 1); rows per identity p50 1, max 3,340. Of the 172,
-- none had a non-synthetic state row before the window and each had at most 2
-- rows in total by the window's end.
--
-- core.agent_state is subject to retention. A later run may return fewer rows
-- and different counts; the recorded result above is the snapshot, and this
-- file documents how it was taken, not a reproducible export.
--
-- Usage:
--   psql -d governance -X -f scripts/analysis/ode_fallback_reach_snapshot.sql

BEGIN TRANSACTION READ ONLY;

-- 1. Window census: identities, fallback-only identities, row counts.
WITH r AS (
  SELECT s.identity_id,
         s.state_json #>> '{eisv_telemetry,measurement,primary,source}' AS src
  FROM core.agent_state s
  WHERE s.recorded_at >= timestamptz '2026-09-18 00:00+00'
    AND s.recorded_at <  timestamptz '2026-09-25 00:00+00'
    AND s.synthetic IS NOT TRUE
    AND s.state_json->'eisv_telemetry'->>'schema' = 'eisv.telemetry.v1'),
per AS (
  SELECT identity_id,
         count(*) AS n,
         count(*) FILTER (WHERE src = 'ode_fallback') AS fb,
         count(*) FILTER (WHERE src = 'behavioral') AS beh,
         count(*) FILTER (WHERE src IS NULL
                          OR src NOT IN ('behavioral', 'ode_fallback')) AS other
  FROM r GROUP BY 1)
SELECT count(*)                                    AS identities,
       count(*) FILTER (WHERE beh = 0)             AS fallback_only,
       count(*) FILTER (WHERE beh = 0 AND n = 1)   AS fallback_only_one_row,
       max(n)   FILTER (WHERE beh = 0)             AS fallback_only_max_rows,
       sum(n)                                      AS rows,
       sum(fb)                                     AS fallback_rows,
       sum(other)                                  AS other_source_rows,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY n) AS p50_rows,
       max(n)                                      AS max_rows
FROM per;

-- 2. Lifetime history of the fallback-only identities, up to the window's end.
WITH r AS (
  SELECT s.identity_id,
         s.state_json #>> '{eisv_telemetry,measurement,primary,source}' AS src
  FROM core.agent_state s
  WHERE s.recorded_at >= timestamptz '2026-09-18 00:00+00'
    AND s.recorded_at <  timestamptz '2026-09-25 00:00+00'
    AND s.synthetic IS NOT TRUE
    AND s.state_json->'eisv_telemetry'->>'schema' = 'eisv.telemetry.v1'),
fallback_only AS (
  SELECT identity_id FROM r GROUP BY 1
  HAVING count(*) FILTER (WHERE src = 'behavioral') = 0),
hist AS (
  SELECT f.identity_id,
         count(s.*) FILTER (WHERE s.recorded_at < timestamptz '2026-09-18 00:00+00')
           AS rows_before_window,
         count(s.*) FILTER (WHERE s.recorded_at < timestamptz '2026-09-25 00:00+00')
           AS rows_to_window_end
  FROM fallback_only f
  LEFT JOIN core.agent_state s
    ON s.identity_id = f.identity_id AND s.synthetic IS NOT TRUE
  GROUP BY 1)
SELECT count(*)                                         AS fallback_only,
       count(*) FILTER (WHERE rows_before_window > 0)   AS with_rows_before_window,
       max(rows_to_window_end)                          AS max_rows_to_window_end
FROM hist;

ROLLBACK;
