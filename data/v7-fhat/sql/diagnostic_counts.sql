-- v7 F-hat Session 1a — diagnostic counts that produced the blocker findings.
-- Re-run any of these to reproduce the audit.

-- Q1: epoch distribution of core.agent_state.
-- Expected shape 2026-04-23: epoch=1 spans 2025-12-12..2026-03-22, epoch=2 spans 2026-04-01..present.
SELECT epoch, COUNT(*) AS rows, COUNT(DISTINCT identity_id) AS agents,
       MIN(recorded_at) AS earliest, MAX(recorded_at) AS latest
FROM core.agent_state GROUP BY epoch ORDER BY epoch;

-- Q2: spec's reference window with spec's epoch filter → expected rows ~17,654 per spec §2.2.
-- Actual: 0 rows.
SELECT COUNT(*) AS total_rows
FROM core.agent_state s
WHERE s.recorded_at BETWEEN '2026-02-20' AND '2026-03-20' AND s.epoch = 2;

-- Q3: same window, drop the epoch filter.
-- Actual: 114,883 rows (~6.5x the spec's 17,654).
SELECT COUNT(*) AS total_rows
FROM core.agent_state s
WHERE s.recorded_at >= '2026-02-20' AND s.recorded_at < '2026-03-20';

-- Q4: audit.events C6-type coverage IN the reference window → expected 71/2729/252 per spec §2.2.
-- Actual: 0 rows across all three types.
SELECT event_type, COUNT(*)
FROM audit.events
WHERE ts >= '2026-02-20' AND ts < '2026-03-20'
  AND event_type IN ('circuit_breaker_trip', 'stuck_detected', 'anomaly_detected')
GROUP BY 1;

-- Q5: first-appearance dates for the three C6 event types (global).
-- Confirms they didn't exist in the reference window.
SELECT event_type, COUNT(*), MIN(ts) AS earliest, MAX(ts) AS latest
FROM audit.events
WHERE event_type IN ('circuit_breaker_trip', 'stuck_detected', 'anomaly_detected')
GROUP BY event_type ORDER BY earliest;

-- Q6: inspect outcome_events.detail for BED |Δη| reconstructability.
-- Detail contains {source, prev_norm, current_norm, norm_delta, prev_verdict}.
-- current_norm is |η_t| (scalar). |Δη_t| requires the vector η_t - η_{t-1}, which is NOT stored.
SELECT DISTINCT jsonb_object_keys(detail) AS detail_key
FROM audit.outcome_events
WHERE detail != '{}'
ORDER BY detail_key;

-- Q7: state_json key inventory. Confirms no η vector components are stored.
-- Keys: E, phi, verdict, risk_score, health_status.
SELECT jsonb_object_keys(state_json) AS key, COUNT(*)
FROM core.agent_state
WHERE state_json != '{}' AND recorded_at > now() - interval '7 days'
GROUP BY key ORDER BY 2 DESC;
