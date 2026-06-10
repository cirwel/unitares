#!/usr/bin/env bash
# wave-0-retention-purge.sh — 90-day retention for audit.coordination_measurements.
#
# ADAPTATION (council-reviewed in the §14 #3+#6 PR): the Wave 3 RFC §6.1
# sketch named a partition-roll script, but migration 041 deliberately
# shipped the measurements table FLAT ("flat table, not partitioned" — see
# 041's header; volume is low and a future migration can swap to RANGE
# partitioning without changing column shape). Retention on a flat table is
# a batched DELETE, not a partition detach. audit.coordination_events (the
# failure channel) IS partitioned and is NOT this script's concern.
#
# Batched (50k/loop) so the purge never takes a long lock; daily via
# com.unitares.wave0-retention-purge.plist at 02:30.
#
# Usage: wave-0-retention-purge.sh [--dry-run] [RETENTION]  (default '90 days')
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
RETENTION="${1:-90 days}"
DSN="${GOVERNANCE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/governance}"

eligible=$(psql "$DSN" -tAc "
  SELECT count(*) FROM audit.coordination_measurements
  WHERE recorded_at < now() - interval '$RETENTION'")
echo "[wave0-purge] rows older than $RETENTION: $eligible"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[wave0-purge] dry-run — no deletion"
  exit 0
fi

total=0
while :; do
  deleted=$(psql "$DSN" -tAc "
    WITH del AS (
      DELETE FROM audit.coordination_measurements
      WHERE id IN (
        SELECT id FROM audit.coordination_measurements
        WHERE recorded_at < now() - interval '$RETENTION'
        LIMIT 50000
      )
      RETURNING 1
    ) SELECT count(*) FROM del")
  total=$((total + deleted))
  [[ "$deleted" -eq 0 ]] && break
done

echo "[wave0-purge] deleted $total rows (retention $RETENTION)"
