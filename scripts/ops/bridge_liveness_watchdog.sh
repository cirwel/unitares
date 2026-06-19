#!/usr/bin/env bash
# Liveness watchdog for the UNITARES Discord bridge (com.unitares.discord-bridge).
#
# Why this exists: launchd KeepAlive only restarts the bridge if the PROCESS
# dies. On 2026-06-19 the bridge HUNG — process alive, async event loop wedged,
# log silent and no governance polling for ~5h — so #alerts delivery was
# silently dead while the service "looked up". KeepAlive cannot see a hang. This
# is the watcher for the watcher: it detects a wedged event loop and restarts.
#
# Signal: the bridge polls governance every EVENT_POLL_INTERVAL (10s) and logs
# each poll (httpx INFO), so a healthy bridge writes its log at least every ~10s.
# If the log is stale beyond STALE_THRESHOLD_S the poll loop is hung. (This
# leans on httpx INFO-level poll logging — the bridge default. A heartbeat file
# written from the poll loop would be a verbosity-independent upgrade; noted as a
# follow-up rather than a bridge code change here.)
#
# Debounced: requires CONSEC consecutive stale observations before acting, so a
# log-rotation blip or brief quiet never triggers a needless restart. One-shot;
# scheduled via com.unitares.bridge-liveness-watchdog.plist.template. On a
# confirmed hang it restarts the bridge FIRST (restoring delivery), then emits
# the alert through the same path as the lease-plane health check — a
# severity=critical governance finding (-> the bridge's own #alerts) plus the
# always-written log floor, so the alert lands once the bridge is back.

set -uo pipefail

BRIDGE_LABEL="${BRIDGE_LABEL:-com.unitares.discord-bridge}"
BRIDGE_LOG="${BRIDGE_LOG:-$HOME/Library/Logs/unitares-discord-bridge.log}"
# Preferred liveness signal: a heartbeat the bridge's poll loop rewrites every
# iteration (verbosity-independent). Falls back to the bridge log's mtime when
# the heartbeat file is absent (bridge not yet upgraded to write it).
BRIDGE_HEARTBEAT_FILE="${BRIDGE_HEARTBEAT_PATH:-$HOME/.unitares/discord-bridge.heartbeat}"
STALE_THRESHOLD_S="${BRIDGE_STALE_THRESHOLD_S:-180}"   # 18x the 10s poll interval
CONSEC="${BRIDGE_WATCHDOG_CONSEC:-2}"
STATE_FILE="${BRIDGE_WATCHDOG_STATE:-$HOME/.unitares/bridge-watchdog.state}"
ALERT_LOG="${UNITARES_ALERT_LOG:-/tmp/unitares_alerts.log}"
SECRETS_FILE="${UNITARES_SECRETS_ENV:-$HOME/.config/cirwel/secrets.env}"
GOV_API_URL="${UNITARES_GOVERNANCE_HTTP_URL:-http://127.0.0.1:8767}"
TIMEOUT_S="${HEALTHCHECK_TIMEOUT_S:-5}"
# Stubbable so tests can assert the restart fires without touching launchd.
RESTART_CMD="${BRIDGE_RESTART_CMD:-launchctl kickstart -k gui/$(id -u)/$BRIDGE_LABEL}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
now() { date '+%s'; }

read_count() {
  local n; n="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$n" in (*[!0-9]*|"") n=0 ;; esac
  printf '%s' "$n"
}
write_count() { mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true; printf '%s' "$1" >"$STATE_FILE" 2>/dev/null || true; }

alert() { echo "[$(ts)] ALERT: $1" | tee -a "$ALERT_LOG" >&2; }

HTTP_API_TOKEN="$( ( [ -f "$SECRETS_FILE" ] && set -a && . "$SECRETS_FILE" >/dev/null 2>&1; printf '%s' "${UNITARES_HTTP_API_TOKEN:-}" ) || true )"
post_finding() {
  local severity="$1" fingerprint="$2" message="$3" payload
  payload=$(python3 -c '
import json,sys
print(json.dumps({
  "type": "bridge_liveness_finding",
  "severity": sys.argv[1], "message": sys.argv[2],
  "agent_id": "bridge-liveness-watchdog", "agent_name": "bridge-liveness-watchdog",
  "fingerprint": sys.argv[3],
}))' "$severity" "$message" "$fingerprint" 2>/dev/null) || return 0
  curl -s --max-time "$TIMEOUT_S" -o /dev/null \
    ${HTTP_API_TOKEN:+-H "Authorization: Bearer $HTTP_API_TOKEN"} \
    -H "Content-Type: application/json" \
    -X POST "$GOV_API_URL/api/findings" -d "$payload" 2>/dev/null || true
}

# Seconds since the bridge last proved liveness. Prefer the poll-loop heartbeat
# (verbosity-independent); fall back to the log mtime when the heartbeat file is
# absent (bridge not yet upgraded). An unreadable source yields a non-stale
# value so a stat hiccup can't trigger a spurious restart.
liveness_age_s() {
  local src mt
  if [ -f "$BRIDGE_HEARTBEAT_FILE" ]; then src="$BRIDGE_HEARTBEAT_FILE"; else src="$BRIDGE_LOG"; fi
  mt="$(stat -f %m "$src" 2>/dev/null || echo 0)"
  [ "$mt" = "0" ] && { printf '%s' "0"; return; }   # unknown -> treat as fresh, not stale
  printf '%s' "$(( $(now) - mt ))"
}

age=$(liveness_age_s)

if [ "$age" -le "$STALE_THRESHOLD_S" ]; then
  prev=$(read_count)
  write_count 0
  if [ "$prev" -ge "$CONSEC" ]; then
    rec="RECOVERED: Discord bridge is alive again (liveness age ${age}s) after a wedge — #alerts delivery restored"
    alert "$rec"
    post_finding "info" "bridge-liveness-recovery" "$rec"
  fi
  echo "[$(ts)] bridge liveness OK (age ${age}s)"
  exit 0
fi

# stale
n=$(( $(read_count) + 1 ))
write_count "$n"
echo "[$(ts)] bridge liveness STALE ${age}s (>${STALE_THRESHOLD_S}s) — strike $n/$CONSEC" >&2
if [ "$n" -lt "$CONSEC" ]; then
  exit 1   # not yet confirmed; let the next run decide (debounce)
fi

# confirmed wedge: restart FIRST so the alert can be delivered, then alert.
echo "[$(ts)] bridge appears wedged (liveness stale ${age}s, ${n}x) — restarting $BRIDGE_LABEL" >&2
eval "$RESTART_CMD" >/dev/null 2>&1 || true
msg="Discord bridge wedged — process alive but event loop silent for ${age}s (no governance polling, #alerts delivery dead). Restarted $BRIDGE_LABEL. KeepAlive can't catch a hang; this watchdog did."
alert "$msg"
post_finding "critical" "bridge-liveness-wedge" "$msg"
exit 1
