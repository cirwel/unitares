#!/usr/bin/env bash
# schema_drift_check.sh — Wave 3 §8.1 drift gate.
#
# core.identities_shadow / core.agents_shadow are created with
# `LIKE <canonical> INCLUDING ALL` (migrations 043/044). If the canonical
# table's shape later changes without a paired shadow migration, the §8.2
# comparator silently compares against a stale shape. This script fails when
# the column shape (name + data type, ordinal order) of a canonical table
# diverges from its shadow (excluding the shadow-only `shadow_write_at`).
#
# Exit codes:
#   0 — shapes match
#   1 — DRIFT detected (fail the gate)
#   2 — no database reachable (distinct from drift so CI wiring can decide;
#       a silent pass without a DB would be a false gate)
#
# Usage: db/postgres/schema_drift_check.sh
#   Honors GOVERNANCE_DATABASE_URL (default: local governance DB).

set -euo pipefail

DSN="${GOVERNANCE_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/governance}"

if ! psql "$DSN" -tAc "select 1" >/dev/null 2>&1; then
  echo "[schema-drift] no database reachable at \$GOVERNANCE_DATABASE_URL — cannot verify (exit 2)" >&2
  exit 2
fi

shape() { # shape <table_name> <exclude_column_or_empty>
  local table="$1" exclude="${2:-}"
  psql "$DSN" -tAc "
    SELECT column_name || ':' || data_type
    FROM information_schema.columns
    WHERE table_schema = 'core'
      AND table_name = '$table'
      AND column_name <> COALESCE(NULLIF('$exclude', ''), '__none__')
    ORDER BY ordinal_position"
}

drift=0
for pair in identities agents; do
  shadow="${pair}_shadow"
  if [[ -z "$(psql "$DSN" -tAc "select to_regclass('core.$shadow')")" ]]; then
    echo "[schema-drift] core.$shadow does not exist — apply migrations 043/044 first" >&2
    drift=1
    continue
  fi
  if ! diff <(shape "$pair") <(shape "$shadow" "shadow_write_at") >/tmp/schema_drift_$pair.diff 2>&1; then
    echo "[schema-drift] DRIFT between core.$pair and core.$shadow:" >&2
    cat /tmp/schema_drift_$pair.diff >&2
    drift=1
  else
    echo "[schema-drift] core.$pair == core.$shadow (modulo shadow_write_at)"
  fi
done

exit $drift
