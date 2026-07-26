#!/usr/bin/env bash
# Liveness watchdog for the UNITARES Discord bridge (com.unitares.discord-bridge).
#
# Why this exists: launchd KeepAlive only restarts the bridge if the PROCESS
# dies. On 2026-06-19 the bridge HUNG — process alive, async event loop wedged,
# log silent and no governance polling for ~5h — so #alerts delivery was
# silently dead while the service "looked up". KeepAlive cannot see a hang. This
# is the watcher for the watcher: it detects a wedged event loop and restarts.
#
# Signal: the bridge's poll loop rewrites a heartbeat file every iteration
# (bridge PR #24) — verbosity-independent. The log mtime remains only as a
# fallback for a bridge that predates the heartbeat; note that log rotation
# can leave the bridge writing to an unlinked inode, making the visible log's
# mtime permanently stale, so the heartbeat is the trustworthy signal.
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
# Ongoing-wedge state: "<first_confirmed_epoch> <last_alert_epoch>". Repeat
# criticals for the SAME continuing wedge are rate-limited to one per
# ALERT_COOLDOWN_S (the 2026-07 wedge posted ~12 identical criticals/day for
# six days). The per-run log line still records every stale observation.
WEDGE_STATE_FILE="${BRIDGE_WATCHDOG_WEDGE_STATE:-$HOME/.unitares/bridge-watchdog.wedge}"
ALERT_COOLDOWN_S="${BRIDGE_WATCHDOG_ALERT_COOLDOWN_S:-21600}"
RESTART_SETTLE_S="${BRIDGE_RESTART_SETTLE_S:-3}"
ALERT_LOG="${UNITARES_ALERT_LOG:-/tmp/unitares_alerts.log}"
SECRETS_FILE="${UNITARES_SECRETS_ENV:-$HOME/.config/cirwel/secrets.env}"
GOV_API_URL="${UNITARES_GOVERNANCE_HTTP_URL:-http://127.0.0.1:8767}"
TIMEOUT_S="${HEALTHCHECK_TIMEOUT_S:-5}"
# Stubbable so tests can assert the restart fires without touching launchd.
RESTART_CMD="${BRIDGE_RESTART_CMD:-launchctl kickstart -k gui/$(id -u)/$BRIDGE_LABEL}"
# Stubbable PID probe so tests can simulate restart success/failure.
PID_CMD="${BRIDGE_PID_CMD:-}"

bridge_pid() {
  if [ -n "$PID_CMD" ]; then eval "$PID_CMD" 2>/dev/null; return; fi
  launchctl list 2>/dev/null | awk -v l="$BRIDGE_LABEL" '$3==l && $1 ~ /^[0-9]+$/ {print $1; exit}'
}

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
  rm -f "$WEDGE_STATE_FILE" 2>/dev/null || true
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
pid_before="$(bridge_pid)"
eval "$RESTART_CMD" >/dev/null 2>&1 || true
sleep "$RESTART_SETTLE_S"
pid_after="$(bridge_pid)"

# Say what actually happened. The 2026-07 wedge ran six days with every
# alert claiming "Restarted" while kickstart silently changed nothing —
# never report a restart as done without evidence the pid changed.
if [ -n "$pid_before" ] && [ "$pid_before" = "$pid_after" ]; then
  restart_desc="restart FAILED — pid $pid_after unchanged after kickstart; MANUAL restart needed: launchctl kickstart -k gui/$(id -u)/$BRIDGE_LABEL"
elif [ -n "$pid_after" ]; then
  restart_desc="restarted $BRIDGE_LABEL (pid ${pid_before:-none} -> $pid_after)"
else
  restart_desc="restart issued but bridge pid unknown — verify $BRIDGE_LABEL manually"
fi

now_s=$(now)
first_confirmed="$now_s"; last_alert=0
if [ -f "$WEDGE_STATE_FILE" ]; then
  read -r first_confirmed last_alert < "$WEDGE_STATE_FILE" 2>/dev/null || true
  case "$first_confirmed" in (*[!0-9]*|"") first_confirmed="$now_s" ;; esac
  case "$last_alert" in (*[!0-9]*|"") last_alert=0 ;; esac
fi

msg="Discord bridge wedged — process alive but event loop silent for ${age}s (no governance polling, #alerts delivery dead; wedge first confirmed $(date -r "$first_confirmed" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "epoch $first_confirmed")). ${restart_desc}."
alert "$msg"
if [ $(( now_s - last_alert )) -ge "$ALERT_COOLDOWN_S" ]; then
  post_finding "critical" "bridge-liveness-wedge" "$msg"
  last_alert="$now_s"
else
  echo "[$(ts)] wedge continues; governance alert suppressed (cooldown $(( ALERT_COOLDOWN_S - (now_s - last_alert) ))s remaining)" >&2
fi
mkdir -p "$(dirname "$WEDGE_STATE_FILE")" 2>/dev/null || true
printf '%s %s' "$first_confirmed" "$last_alert" >"$WEDGE_STATE_FILE" 2>/dev/null || true
exit 1
