-- v7 F-hat Session 1 — reference corpus: event stream (C6)
-- Spec: §2.2 C6 (audit.events)
--
-- HARD BLOCKER — see session1a-findings.md §3.
-- All three C6 event types (circuit_breaker_trip, stuck_detected, anomaly_detected)
-- first appeared in audit.events AFTER 2026-04-11, well after the reference window
-- (2026-02-20 through 2026-03-20) closes. This SQL returns zero rows when run
-- against the spec's reference window. Running it confirms the blocker.
--
-- Global first-appearance dates from audit.events:
--   stuck_detected       : 2026-04-11
--   anomaly_detected     : 2026-04-12
--   circuit_breaker_trip : 2026-04-16
--
-- Consequence: 9 C6 emission parameters per class (18 total) are structurally
-- unidentifiable from the reference window. Session 1 fit is blocked on operator
-- redirect per findings §3.

SELECT
    e.ts,
    e.event_id,
    e.agent_id,
    e.event_type,
    e.confidence,
    e.payload
FROM audit.events e
WHERE e.ts >= :'window_start'::timestamptz
  AND e.ts <  :'window_end'::timestamptz
  AND e.event_type IN ('circuit_breaker_trip', 'stuck_detected', 'anomaly_detected')
ORDER BY e.agent_id, e.ts;
