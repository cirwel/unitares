#!/usr/bin/env bash
# Synthetic end-to-end ACQUIRE health check for the Surface Lease Plane.
#
# Why this exists, separate from a /health liveness ping: the lease plane can be
# "process up" (liveness 200) while every real acquire silently fails. That is
# exactly the 2026-06-19 outage — acquires returned HTTP 000 for ~4 days because
# the deploy checkout's deps/ was incomplete (plug/postgrex missing), and the
# file-lease hook fails OPEN, so nothing alerted. "Process up" != "working".
#
# This does a REAL acquire + release round-trip against the running plane and
# alerts when that round-trip breaks — the one signal a liveness ping cannot
# give. Scheduled one-shot (see com.unitares.lease-plane-acquire-healthcheck.
# plist.template); consecutive-failure state persists across runs so a single
# transient blip never pages — it alerts only after MAX_FAILURES in a row, and
# emits a one-shot RECOVERED note when it comes back.
#
# Alert sink matches scripts/ops/monitor_health.sh: a line appended to
# $UNITARES_ALERT_LOG (default /tmp/unitares_alerts.log) and stderr.

# NOT `set -e`: curl/probe failures are handled explicitly via fail(), which must
# still update state and alert rather than abort on the first non-zero command.
set -uo pipefail

BASE_URL="${LEASE_PLANE_BASE_URL:-http://127.0.0.1:8788}"
SECRETS_FILE="${UNITARES_SECRETS_ENV:-$HOME/.config/cirwel/secrets.env}"
STATE_FILE="${LEASE_PLANE_HEALTHCHECK_STATE:-$HOME/.unitares/lease-plane-healthcheck.state}"
ALERT_LOG="${UNITARES_ALERT_LOG:-/tmp/unitares_alerts.log}"
MAX_FAILURES="${MAX_FAILURES:-2}"
TIMEOUT_S="${HEALTHCHECK_TIMEOUT_S:-5}"
# A dedicated probe surface in /tmp — never a real file, so the check can never
# collide with a genuine holder. TTL is short so a crash mid-probe self-reaps.
SURFACE="${HEALTHCHECK_SURFACE:-file:///tmp/lease-plane-acquire-healthcheck}"
# Any valid UUID; fixed so repeated probes are idempotent on the same holder.
HOLDER_UUID="${HEALTHCHECK_HOLDER_UUID:-11111111-1111-1111-1111-111111111111}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

read_failcount() {
  local n
  n="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$n" in (*[!0-9]*|"") n=0 ;; esac   # sanitize to a non-negative integer
  printf '%s' "$n"
}
write_failcount() { mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true; printf '%s' "$1" >"$STATE_FILE" 2>/dev/null || true; }

alert() { echo "[$(ts)] ALERT: $1" | tee -a "$ALERT_LOG" >&2; }

fail() {
  local n; n=$(( $(read_failcount) + 1 ))
  write_failcount "$n"
  echo "[$(ts)] lease-plane acquire probe FAILED ($n/$MAX_FAILURES): $1" >&2
  if [ "$n" -ge "$MAX_FAILURES" ]; then
    alert "lease plane synthetic acquire failing ${n}x consecutively ($BASE_URL): $1 — file-lease coordination is degraded and failing OPEN. Check deps in unitares-deploy/elixir/lease_plane and ~/Library/Logs/unitares-lease-plane.log"
  fi
  exit 1
}

# Pull only the bearer token from secrets (subshell so nothing else leaks).
TOKEN="$( ( [ -f "$SECRETS_FILE" ] && set -a && . "$SECRETS_FILE" >/dev/null 2>&1; printf '%s' "${LEASE_PLANE_BEARER_TOKEN:-}" ) || true )"
[ -z "$TOKEN" ] && fail "no LEASE_PLANE_BEARER_TOKEN in $SECRETS_FILE"

# --- end-to-end probe: acquire -> assert ok -> release ---
body=$(printf '{"surface_id":"%s","holder_agent_uuid":"%s","holder_kind":"remote_heartbeat","ttl_s":30,"holder_class":"process_instance","holder_pid":"%s","intent":"healthcheck","audit_session":"healthcheck"}' "$SURFACE" "$HOLDER_UUID" "$$")

resp=$(curl -s --max-time "$TIMEOUT_S" -w $'\n%{http_code}' \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST "$BASE_URL/v1/lease/acquire" -d "$body" 2>/dev/null)
code=$(printf '%s' "$resp" | tail -1)
json=$(printf '%s' "$resp" | sed '$d')

[ "$code" = "200" ] || fail "acquire HTTP=$code (expected 200; 000=connection reset/crash, 503=fail-closed auth)"

ok=$(printf '%s' "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok'))" 2>/dev/null || echo "parse_error")
[ "$ok" = "True" ] || fail "acquire ok=$ok body=$(printf '%s' "$json" | head -c 140)"

lease_id=$(printf '%s' "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('lease',{}).get('lease_id',''))" 2>/dev/null || echo "")

# Release best-effort — a release hiccup is not a health failure (the 30s TTL
# reaps the probe lease either way).
[ -n "$lease_id" ] && curl -s --max-time "$TIMEOUT_S" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST "$BASE_URL/v1/lease/release" -d "{\"lease_id\":\"$lease_id\"}" >/dev/null 2>&1

prev=$(read_failcount)
write_failcount 0
if [ "$prev" -ge "$MAX_FAILURES" ]; then
  alert "RECOVERED: lease plane synthetic acquire succeeding again after $prev consecutive failures"
fi
echo "[$(ts)] lease-plane acquire probe OK (lease_id=${lease_id:-?})"
exit 0
