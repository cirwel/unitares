-- Operational supply audit for #2818; no outcome-discrimination analysis.
-- psql -X -v ON_ERROR_STOP=1 -d <database> -f audit.sql
-- Fixed windows are historical. Retention can change a later rerun's counts.
\pset pager off
\pset format unaligned
\pset footer off
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '20s';
SET LOCAL TIME ZONE 'UTC';

\echo projection_snapshot
SELECT updated_at, version,
       (SELECT count(*) FROM jsonb_object_keys(data->'tactical_bins_by_agent')) AS tactical_agents,
       (SELECT count(*) FROM jsonb_object_keys(data->'bins_by_agent')) AS strategic_agents,
       jsonb_typeof(data->'tactical_bins_by_agent') AS tactical_map_type
FROM core.calibration;

\echo channel_coverage_24h
WITH rows AS (
    SELECT state_json->'eisv_telemetry' AS t
    FROM core.agent_state
    WHERE recorded_at >= '2026-11-09T08:20:00Z'
      AND recorded_at < '2026-11-10T08:20:00Z'
)
SELECT t#>>'{derivation,kind}' AS kind,
       t#>>'{measurement,agent_calibration,evidence_status}' AS evidence_status,
       t#>>'{measurement,agent_calibration,default_reason}' AS default_reason,
       t#>>'{measurement,agent_calibration,n_verified_pairs}' AS pairs,
       count(*) AS rows
FROM rows GROUP BY 1,2,3,4 ORDER BY rows DESC, kind;

\echo i_base_components_24h
WITH rows AS (
    SELECT state_json->'eisv_telemetry' AS t
    FROM core.agent_state
    WHERE recorded_at >= '2026-11-09T08:20:00Z'
      AND recorded_at < '2026-11-10T08:20:00Z'
      AND state_json#>>'{eisv_telemetry,derivation,kind}' = 'behavioral_sensor'
)
SELECT jsonb_typeof(t#>'{measurement,agent_calibration}') AS channel_type,
       t#>>'{measurement,agent_calibration,n_verified_pairs}' AS pairs,
       c->>'name' AS component, c->>'source' AS source,
       c->>'observed' AS observed, c->>'default_reason' AS default_reason,
       count(*) AS rows
FROM rows
LEFT JOIN LATERAL jsonb_array_elements(
    t#>'{derivation,components,dimensions,I,components}'
) c ON TRUE
GROUP BY 1,2,3,4,5,6 ORDER BY rows DESC, component;

\echo outcome_supply_7d
SELECT outcome_type, verification_source,
       detail->>'corroboration_grade' AS grade,
       detail->>'evidence_weight' AS weight,
       detail->>'prediction_binding' AS binding,
       detail->>'calibration_excluded' AS excluded,
       detail->>'calibration_exclusion_reasons' AS exclusion_reasons,
       count(*) AS rows
FROM audit.outcome_events
WHERE ts >= '2026-11-03T08:20:00Z' AND ts < '2026-11-10T08:20:00Z'
GROUP BY 1,2,3,4,5,6,7 ORDER BY rows DESC, outcome_type, binding, grade;

\echo external_producers_7d
SELECT detail->>'recorded_via' AS recorded_via,
       detail->>'classifier' AS classifier,
       detail->>'prediction_source' AS prediction_source,
       count(*) AS rows
FROM audit.outcome_events
WHERE ts >= '2026-11-03T08:20:00Z' AND ts < '2026-11-10T08:20:00Z'
  AND verification_source = 'external_signal'
GROUP BY 1,2,3 ORDER BY rows DESC;

\echo delivery_snapshot
SELECT count(*) AS rows,
       count(*) FILTER (WHERE completed_at IS NULL) AS pending,
       max(completed_at) AS newest_completed
FROM audit.outcome_calibration_deliveries;
COMMIT;
