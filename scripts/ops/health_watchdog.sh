#!/bin/bash
# UNITARES + Anima health watchdog
# Runs every 5 minutes via launchd. Logs failures to /tmp/unitares_health.log.
# Exits silently on success — only writes when something is wrong.
#
# The probe roster is NOT maintained here. It comes from the port registry in
# scripts/dev/ports_catalog.py, because a hand-kept list is how port 8768
# crash-looped for eight days unnoticed: this script checked two services while
# the registry declared six, and the hourly deploy doctor reported every surface
# healthy — truthfully, because 8768 was not one of its surfaces.
#
# Add a service to PORTS with a `health` block and it is monitored from the next
# run. Forget to, and tests/test_ports_catalog_health_coverage.py fails.

LOG="/tmp/unitares_health.log"
MAX_LOG_LINES=500
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${UNITARES_PYTHON:-python3}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Probe one URL. Unlike a bare 200 test, `accepted` is a comma-separated list:
# the orchestrator is bearer-gated and answers 401 to an unauthenticated probe,
# which means alive. A dead port answers neither — curl reports 000.
check() {
    local name="$1" url="$2" accepted="${3:-200}" timeout="${4:-5}"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$timeout" "$url" 2>/dev/null)
    case ",$accepted," in
        *",$code,"*) return 0 ;;
    esac
    echo "[$(ts)] FAIL $name — HTTP $code, wanted one of $accepted ($url)" >> "$LOG"
    return 1
}

failures=0

# --- roster, from the registry ------------------------------------------------
roster=""
if [ -f "$REPO/scripts/dev/ports_catalog.py" ]; then
    roster=$("$PYTHON" "$REPO/scripts/dev/ports_catalog.py" --health-probes 2>/dev/null)
fi

if [ -n "$roster" ]; then
    while IFS=$'\t' read -r name url accepted; do
        [ -n "$url" ] || continue
        check "$name" "$url" "$accepted" || failures=$((failures + 1))
    done <<< "$roster"
else
    # Never degrade silently — an empty roster would otherwise read as "nothing
    # is wrong" while monitoring nothing at all, which is the exact failure this
    # script exists to catch.
    echo "[$(ts)] FAIL health-watchdog roster — ports_catalog.py produced no probes (repo: $REPO); falling back to governance only" >> "$LOG"
    failures=$((failures + 1))
    check "governance (fallback)" "${GOVERNANCE_HEALTH_URL:-http://localhost:8767/health}" 200 \
        || failures=$((failures + 1))
fi

# --- edge node, opt-in --------------------------------------------------------
# Anima runs on a separate host (e.g. a Pi over Tailscale), so it is not in the
# governance-host roster. Set ANIMA_HEALTH_URL to enable; unset disables, because
# the localhost default rarely runs Anima.
if [ -n "$ANIMA_HEALTH_URL" ]; then
    check "anima" "$ANIMA_HEALTH_URL" 200 10 || failures=$((failures + 1))
fi

# --- PostgreSQL, via the governance health detail -----------------------------
GOVERNANCE_HEALTH_URL="${GOVERNANCE_HEALTH_URL:-http://localhost:8767/health}"
if [ $failures -eq 0 ]; then
    db_status=$(curl -s --max-time 5 "$GOVERNANCE_HEALTH_URL" 2>/dev/null | "$PYTHON" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('database', {}).get('status', 'unknown'))
except Exception:
    print('unknown')
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
