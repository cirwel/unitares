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
# Auto-remediation (#1277). The 2026-06-27 outage failed OPEN for ~5.4h: this
# probe paged 66x but nothing restarted the plane. start.sh self-heals (mix
# deps.get) on boot, so a restart is the fix — the deploy never triggers one.
# OPT-IN (default off): set LEASE_PLANE_HEALTHCHECK_AUTORESTART=1 to enable. When
# on, after REMEDIATE_FAILURES consecutive failures we issue ONE `launchctl
# kickstart -k` per outage streak (the same mechanism deploy-lease-plane.sh uses)
# and let the next probe confirm recovery. One-shot per streak (RESTART_MARKER,
# cleared on recovery) so a persistent fault never becomes a restart storm.
AUTORESTART="${LEASE_PLANE_HEALTHCHECK_AUTORESTART:-0}"
REMEDIATE_FAILURES="${REMEDIATE_FAILURES:-3}"
LEASE_PLANE_LABEL="${LEASE_PLANE_LABEL:-com.unitares.lease-plane}"
RESTART_MARKER="${STATE_FILE}.restart-attempted"
# A dedicated probe surface in /tmp — never a real file, so the check can never
# collide with a genuine holder. TTL is short so a crash mid-probe self-reaps.
SURFACE="${HEALTHCHECK_SURFACE:-file:///tmp/lease-plane-acquire-healthcheck}"
# Any valid UUID; fixed so repeated probes are idempotent on the same holder.
HOLDER_UUID="${HEALTHCHECK_HOLDER_UUID:-11111111-1111-1111-1111-111111111111}"
# Governance HTTP API — POST /api/findings emits into the event ring buffer the
# Discord bridge polls; a `*_finding` at severity=critical pages the bridge's
# #alerts channel. Best-effort surfacing on top of the always-written log; the
# log stays the floor in case governance itself is down.
GOV_API_URL="${UNITARES_GOVERNANCE_HTTP_URL:-http://127.0.0.1:8767}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

read_failcount() {
  local n
  n="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$n" in (*[!0-9]*|"") n=0 ;; esac   # sanitize to a non-negative integer
  printf '%s' "$n"
}
write_failcount() { mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true; printf '%s' "$1" >"$STATE_FILE" 2>/dev/null || true; }

alert() { echo "[$(ts)] ALERT: $1" | tee -a "$ALERT_LOG" >&2; }

# Surface to Discord via the governance event ring buffer (the bridge polls it).
# Best-effort: a failure here never affects the check's exit status — the log
# line above already recorded the alert. fingerprint dedupes, so a persistent
# outage pages #alerts once, not every interval. $HTTP_API_TOKEN may be empty
# (the API is open on localhost when no token is configured).
post_finding() {
  local severity="$1" fingerprint="$2" message="$3"
  local payload
  payload=$(python3 -c '
import json,sys
print(json.dumps({
  "type": "lease_plane_health_finding",
  "severity": sys.argv[1],
  "message": sys.argv[2],
  "agent_id": "lease-plane-healthcheck",
  "agent_name": "lease-plane-healthcheck",
  "fingerprint": sys.argv[3],
}))' "$severity" "$message" "$fingerprint" 2>/dev/null) || return 0
  curl -s --max-time "$TIMEOUT_S" -o /dev/null \
    ${HTTP_API_TOKEN:+-H "Authorization: Bearer $HTTP_API_TOKEN"} \
    -H "Content-Type: application/json" \
    -X POST "$GOV_API_URL/api/findings" -d "$payload" 2>/dev/null || true
}

# Auto-restart the plane once per outage streak (#1277). `kickstart -k` restarts
# the LaunchAgent in place; start.sh re-runs `mix deps.get` on boot, healing the
# missing-deps fault that causes the fail-open. Best-effort — a restart failure
# is itself surfaced; the next probe verifies recovery and clears the marker.
maybe_remediate() {
  local n="$1"
  [ "$AUTORESTART" = "1" ] || return 0
  [ "$n" -ge "$REMEDIATE_FAILURES" ] || return 0
  if [ -f "$RESTART_MARKER" ]; then
    echo "[$(ts)] auto-restart already attempted this streak; awaiting recovery / manual intervention" >&2
    return 0
  fi
  : >"$RESTART_MARKER" 2>/dev/null || true
  local domain="gui/$(id -u)"
  echo "[$(ts)] auto-remediating: launchctl kickstart -k $domain/$LEASE_PLANE_LABEL" >&2
  if launchctl kickstart -k "$domain/$LEASE_PLANE_LABEL" 2>/dev/null; then
    local m="auto-restart of $LEASE_PLANE_LABEL issued after ${n} consecutive acquire failures (start.sh re-runs mix deps.get); next probe verifies recovery"
    alert "$m"; post_finding "info" "lease-plane-acquire-autorestart" "$m"
  else
    local m="auto-restart FAILED: launchctl kickstart -k $domain/$LEASE_PLANE_LABEL returned nonzero — manual bootout+bootstrap needed"
    alert "$m"; post_finding "critical" "lease-plane-acquire-autorestart-failed" "$m"
  fi
}

fail() {
  local n; n=$(( $(read_failcount) + 1 ))
  write_failcount "$n"
  echo "[$(ts)] lease-plane acquire probe FAILED ($n/$MAX_FAILURES): $1" >&2
  if [ "$n" -ge "$MAX_FAILURES" ]; then
    local msg="lease plane synthetic acquire failing ${n}x consecutively ($BASE_URL): $1 — file-lease coordination is degraded and failing OPEN. Check deps in unitares-deploy/elixir/lease_plane and ~/Library/Logs/unitares-lease-plane.log"
    alert "$msg"
    post_finding "critical" "lease-plane-acquire-outage" "$msg"
  fi
  maybe_remediate "$n"
  exit 1
}

# Pull the bearer tokens from secrets (subshell so nothing else leaks). The
# lease bearer authenticates the probe; the HTTP API token (optional — the API
# is open on localhost without one) authenticates the Discord-surfacing finding.
TOKEN="$( ( [ -f "$SECRETS_FILE" ] && set -a && . "$SECRETS_FILE" >/dev/null 2>&1; printf '%s' "${LEASE_PLANE_BEARER_TOKEN:-}" ) || true )"
HTTP_API_TOKEN="$( ( [ -f "$SECRETS_FILE" ] && set -a && . "$SECRETS_FILE" >/dev/null 2>&1; printf '%s' "${UNITARES_HTTP_API_TOKEN:-}" ) || true )"
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
rm -f "$RESTART_MARKER" 2>/dev/null || true   # streak over — re-arm auto-restart
if [ "$prev" -ge "$MAX_FAILURES" ]; then
  rec="RECOVERED: lease plane synthetic acquire succeeding again after $prev consecutive failures"
  alert "$rec"
  # severity=info: shows in the bridge's event feed to close the loop, without
  # re-paging #alerts (only critical / *_finding-high page).
  post_finding "info" "lease-plane-acquire-recovery" "$rec"
fi
echo "[$(ts)] lease-plane acquire probe OK (lease_id=${lease_id:-?})"
exit 0
