#!/usr/bin/env bash
# wave-0-channel-report.sh — Wave 3 §6.5 dual-channel report.
#
# Reads BOTH Wave-0 tables over a stated window and prints:
#   failure panel     — audit.coordination_events: count by event_type
#                       (+ error_class breakdown where the payload carries one)
#   measurement panel — audit.coordination_measurements: count, p50/p99
#                       elapsed_ms, status breakdown, by endpoint
#
# This is what disconfirmer (B) reads against (§0): the measurement panel's
# lease_plane rows are the Phase A baseline; Wave 3's
# measurement.beam_python_boundary.request rows land in the same panel when
# the implementation wires them.
#
# Usage: wave-0-channel-report.sh [WINDOW]     (default: '14 days')
set -euo pipefail

WINDOW="${1:-14 days}"
DSN="${GOVERNANCE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/governance}"

echo "=== Wave 0 channel report — window: last $WINDOW ==="
echo
echo "--- FAILURE panel (audit.coordination_events) ---"
psql "$DSN" -c "
  SELECT event_type,
         count(*)                                   AS events,
         count(DISTINCT agent_id)                   AS agents,
         coalesce(payload->>'error_class','-')      AS error_class,
         count(*)                                   AS by_class
  FROM audit.coordination_events
  WHERE ts > now() - interval '$WINDOW'
  GROUP BY event_type, payload->>'error_class'
  ORDER BY events DESC, by_class DESC
  LIMIT 40"

echo "--- MEASUREMENT panel (audit.coordination_measurements) ---"
psql "$DSN" -c "
  SELECT measurement_type,
         endpoint,
         count(*)                                                       AS requests,
         round(percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms)) AS p50_ms,
         round(percentile_cont(0.99) WITHIN GROUP (ORDER BY elapsed_ms)) AS p99_ms,
         min(recorded_at)::date                                         AS first_row,
         max(recorded_at)::date                                         AS last_row
  FROM audit.coordination_measurements
  WHERE recorded_at > now() - interval '$WINDOW'
  GROUP BY measurement_type, endpoint
  ORDER BY requests DESC
  LIMIT 40"

echo "--- MEASUREMENT status breakdown ---"
psql "$DSN" -c "
  SELECT measurement_type, endpoint, status, count(*) AS n
  FROM audit.coordination_measurements
  WHERE recorded_at > now() - interval '$WINDOW'
  GROUP BY measurement_type, endpoint, status
  ORDER BY n DESC
  LIMIT 40"

echo "--- disconfirmer (B) clock ---"
psql "$DSN" -tAc "
  SELECT CASE
    WHEN min(recorded_at) IS NULL THEN
      'no measurement.lease_plane.request rows yet — 14-day clock NOT started'
    WHEN now() - min(recorded_at) >= interval '14 days' THEN
      '14-day window SATISFIED (first row ' || min(recorded_at)::date || ')'
    ELSE
      'clock running: ' || (now()::date - min(recorded_at)::date) || '/14 days (first row ' || min(recorded_at)::date || ')'
  END
  FROM audit.coordination_measurements
  WHERE measurement_type = 'measurement.lease_plane.request'"
