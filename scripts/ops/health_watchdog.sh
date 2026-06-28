#!/bin/bash
# UNITARES + Anima health watchdog
# Runs every 5 minutes via launchd. Logs failures to /tmp/unitares_health.log.
# Exits silently on success — only writes when something is wrong.

LOG="/tmp/unitares_health.log"
MAX_LOG_LINES=500

ts() { date '+%Y-%m-%d %H:%M:%S'; }

check() {
    local name="$1" url="$2" timeout="${3:-5}"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$timeout" "$url" 2>/dev/null)
    if [ "$code" != "200" ]; then
        echo "[$(ts)] FAIL $name — HTTP $code ($url)" >> "$LOG"
        return 1
    fi
    return 0
}

failures=0

# Governance (Mac local). Override host/port via GOVERNANCE_HEALTH_URL.
GOVERNANCE_HEALTH_URL="${GOVERNANCE_HEALTH_URL:-http://localhost:8767/health}"
check "governance" "$GOVERNANCE_HEALTH_URL" || failures=$((failures + 1))

# Anima edge node (e.g. a Pi over Tailscale). Set ANIMA_HEALTH_URL to your
# node's health endpoint; unset disables the check (localhost default rarely
# runs Anima).
ANIMA_HEALTH_URL="${ANIMA_HEALTH_URL:-http://localhost:8766/health}"
check "anima" "$ANIMA_HEALTH_URL" 10 || failures=$((failures + 1))

# PostgreSQL (via governance health detail)
if [ $failures -eq 0 ]; then
    db_status=$(curl -s --max-time 5 "$GOVERNANCE_HEALTH_URL" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('database', {}).get('status', 'unknown'))
except: print('unknown')
" 2>/dev/null)
    if [ "$db_status" != "connected" ]; then
        echo "[$(ts)] WARN governance db pool: $db_status" >> "$LOG"
        failures=$((failures + 1))
    fi
fi

# Trim log if it gets too long
if [ -f "$LOG" ]; then
    lines=$(wc -l < "$LOG")
    if [ "$lines" -gt "$MAX_LOG_LINES" ]; then
        tail -n "$MAX_LOG_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
fi

exit 0
