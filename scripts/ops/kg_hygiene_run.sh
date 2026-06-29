#!/bin/bash
# Weekly KG hygiene sweep: prints the backlog/stale-citation/promote digest and
# cools a bounded batch of aged open note/insight rows to `cold` (reversible).
# Triggered by com.unitares.kg-hygiene launchd (Sundays) or run manually.
#
# Reuses scripts/dev/kg_report.py (the report + conservative cooling tool).
# Cooling is the safe subset only: aged note/insight → cold, canonical rows kept
# warm, actionable rows never touched. See that script's header for the contract.
#
# Env knobs (all optional):
#   KG_HYGIENE_PYTHON      interpreter with psycopg2 (default: python3). For the
#                          launchd job set this to the same python the
#                          governance-mcp service runs.
#   UNITARES_ROOT          checkout root (default: $HOME/projects/unitares)
#   KG_HYGIENE_LOG         digest log (default: ~/Library/Logs/unitares-kg-hygiene.log)
#   KG_HYGIENE_APPLY       1 = cool the safe batch, 0 = report only (default: 1)
#   KG_HYGIENE_COOL_LIMIT  max rows cooled per run, oldest first (default: 20).
#                          Bounds the sweep so the tail drains gradually.
#   KG_HYGIENE_COLD_AGE    age threshold in days for cooling (default: 30)

set -u

PY=${KG_HYGIENE_PYTHON:-python3}
ROOT=${UNITARES_ROOT:-$HOME/projects/unitares}
LOG=${KG_HYGIENE_LOG:-$HOME/Library/Logs/unitares-kg-hygiene.log}
APPLY=${KG_HYGIENE_APPLY:-1}
LIMIT=${KG_HYGIENE_COOL_LIMIT:-20}
COLD_AGE=${KG_HYGIENE_COLD_AGE:-30}
TS=$(date '+%Y-%m-%d %H:%M:%S %Z')

cd "$ROOT" || { echo "kg-hygiene: cannot cd $ROOT" >&2; exit 1; }

ARGS=(scripts/dev/kg_report.py --cold-age-days "$COLD_AGE")
if [ "$APPLY" = "1" ]; then
  ARGS+=(--apply --limit "$LIMIT")
fi

{
  echo
  echo "=========================================="
  echo "KG hygiene — $TS"
  echo "  apply=$APPLY limit=$LIMIT cold_age=${COLD_AGE}d py=$PY"
  echo "=========================================="
  "$PY" "${ARGS[@]}"
  echo "[kg-hygiene] exit=$?"
} >> "$LOG" 2>&1

# Surface the tail to launchd stdout too (captured in the -launchd.log).
tail -n 40 "$LOG"
