-- Operational inventory only: no outcome labels, scores, or discrimination.
-- psql -XqAt -v ON_ERROR_STOP=1 -h localhost -U postgres -d governance \
--   -f docs/operations/production-snapshot-counts.sql
-- Optional: -v cutoff='2026-10-18 22:00:00+00'
-- A later read can differ after retention, deletion, or backfill. Preserve the
-- captured JSON alongside its cutoff rather than treating a rerun as immutable.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL TIME ZONE 'UTC';
\if :{?cutoff}
\else
SELECT transaction_timestamp()::text AS cutoff \gset
\endif
WITH p AS (
  SELECT :'cutoff'::timestamptz AS cutoff
), events AS (
  SELECT count(*) AS retained_rows,
         min(ts) AS earliest_event,
         max(ts) AS latest_event,
         count(*) FILTER (WHERE ts > p.cutoff - interval '7 days') AS rows_7d,
         count(DISTINCT agent_id) FILTER (
           WHERE ts > p.cutoff - interval '21 days') AS emitters_21d,
         count(DISTINCT agent_id) FILTER (
           WHERE ts > p.cutoff - interval '7 days') AS emitters_7d,
         count(*) FILTER (WHERE event_type IN (
           'session_resolve_miss_observed', 'cross_device_call')) AS session_and_call_rows,
         -- Historical comparison subset: broadcaster-form payloads only.
         -- Direct fallback/test records without type are also excluded.
         count(*) FILTER (WHERE event_type = 'lifecycle_resumed'
           AND payload ? 'type'
           AND payload->>'reason' NOT LIKE 'Auto-resumed%') AS non_auto_resumes,
         count(*) FILTER (WHERE event_type = 'lifecycle_resumed'
           AND payload ? 'type'
           AND payload->>'reason' LIKE 'Self-recovery%') AS self_recoveries,
         count(*) FILTER (WHERE event_type = 'lifecycle_resumed'
           AND payload ? 'type'
           AND payload->>'reason' LIKE 'Self-recovery:%') AS legacy_reason_self_recoveries
  FROM audit.events CROSS JOIN p WHERE ts <= p.cutoff
), composition AS (
  SELECT event_type, count(*) AS rows
  FROM audit.events CROSS JOIN p WHERE ts <= p.cutoff
  GROUP BY event_type
), agents AS (
  SELECT count(*) AS retained_rows, min(created_at) AS earliest_created_at,
         max(created_at) AS latest_created_at
  FROM core.agents CROSS JOIN p WHERE created_at <= p.cutoff
), states AS (
  SELECT count(*) AS retained_rows, min(recorded_at) AS earliest_recorded_at,
         max(recorded_at) AS latest_recorded_at
  FROM core.agent_state CROSS JOIN p WHERE recorded_at <= p.cutoff
), discoveries AS (
  SELECT count(*) AS retained_rows, min(created_at) AS earliest_created_at,
         max(created_at) AS latest_created_at
  FROM knowledge.discoveries CROSS JOIN p WHERE created_at <= p.cutoff
)
SELECT jsonb_pretty(jsonb_build_object(
  'schema_version', 1,
  'captured_at', transaction_timestamp(),
  'cutoff', (SELECT cutoff FROM p),
  'audit_events', (SELECT to_jsonb(events) FROM events),
  'event_composition', (SELECT jsonb_agg(to_jsonb(composition) ORDER BY rows DESC, event_type) FROM composition),
  'agent_registry', (SELECT to_jsonb(agents) FROM agents),
  'state_observations', (SELECT to_jsonb(states) FROM states),
  'knowledge_discoveries', (SELECT to_jsonb(discoveries) FROM discoveries)
));
COMMIT;
