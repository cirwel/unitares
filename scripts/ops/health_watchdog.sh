#!/bin/bash
# UNITARES + Anima health watchdog
# Runs every 5 minutes via launchd. Logs failures to /tmp/unitares_health.log.
# Exits silently on success — only writes when something is wrong.
#
# The governance-host probe roster is NOT maintained here. It comes from the
# port registry in scripts/dev/ports_catalog.py, because a hand-kept list is how
# port 8768 crash-looped for eight days unnoticed: this script checked two
# services while the registry declared six, and the hourly deploy doctor
# reported every surface healthy — truthfully, because 8768 was not one of its
# surfaces.
#
# Add a service to PORTS with a `health` block and it is monitored from the next
# run. Forget to, and tests/test_ports_catalog_health_coverage.py fails.

LOG="/tmp/unitares_health.log"
MAX_LOG_LINES=500
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${UNITARES_PYTHON:-python3}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Probe one URL. Unlike a bare 200 test, `accepted` is a comma-separated list:
# the agent orchestrator is bearer-gated and answers 401 to an unauthenticated
# probe, which means alive. A dead port answers neither — curl reports 000.
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
governance_ok=0

# --- governance-host roster, from the registry --------------------------------
roster=""
roster_err=""
if [ -f "$REPO/scripts/dev/ports_catalog.py" ]; then
    # mktemp under the private per-user TMPDIR launchd already provides: a
    # predictable /tmp name is symlink-followable by `>`, and /tmp's sticky bit
    # stops deletion of others' files, not creation of your own link.
    # `|| roster_file=/dev/null` keeps an unwritable or full disk on the loud
    # branch rather than aborting the whole run.
    # The trap is armed ONLY on success: with roster_file=/dev/null it would
    # try to unlink the device node on every degraded run — noisy into the
    # error log here, and genuinely destructive anywhere the process can
    # unlink in /dev.
    roster_file=$(mktemp "${TMPDIR:-/tmp}/unitares_roster.XXXXXX") \
        && trap 'rm -f "$roster_file"' EXIT \
        || roster_file=/dev/null
    # stderr is kept, not discarded: when this fails, why it failed is the one
    # thing an operator needs at 3am, and this branch is meant to be the loud one.
    roster_err=$("$PYTHON" "$REPO/scripts/dev/ports_catalog.py" --health-probes 2>&1 >"$roster_file")
    roster=$(cat "$roster_file" 2>/dev/null)
fi

if [ -n "$roster" ]; then
    while IFS=$'\t' read -r name url accepted role; do
        [ -n "$url" ] || continue
        if check "$name" "$url" "$accepted"; then
            # Keyed off the registry's role marker, never off a port literal.
            # Matching the port here would be the same hand-kept coupling this
            # script exists to remove, in a quieter place: if the governance
            # port moved, the pool read below would simply never run and the
            # script would exit 0 having written nothing.
            if [ "$role" = "governance" ]; then
                governance_ok=1
                # `:-` so an operator override still wins. Assigning
                # unconditionally would honour GOVERNANCE_HEALTH_URL only when
                # the registry is UNREADABLE — the inverse of what anyone would
                # predict, and it would point a remote-host override silently
                # back at localhost.
                GOVERNANCE_HEALTH_URL="${GOVERNANCE_HEALTH_URL:-$url}"
            fi
        else
            failures=$((failures + 1))
        fi
    done <<< "$roster"
    if [ "$governance_ok" -eq 0 ]; then
        # A roster with no governance probe means the registry lost its role
        # marker. Silence here would suppress the pool check invisibly.
        echo "[$(ts)] FAIL health-watchdog roster — no probe declares role=governance; the Postgres pool check cannot run" >> "$LOG"
        failures=$((failures + 1))
    fi
else
    # Never degrade silently — an empty roster would otherwise read as "nothing
    # is wrong" while monitoring nothing at all, which is the exact failure this
    # script exists to catch.
    echo "[$(ts)] FAIL health-watchdog roster — ports_catalog.py produced no probes (repo: $REPO): ${roster_err:-no stderr}" >> "$LOG"
    failures=$((failures + 1))
    # The ONLY port literal left in this script, and only reachable when the
    # registry cannot be read. It must also assign the variable: the pool read
    # below consumes it, and leaving it unset here curls an empty URL and
    # reports the pool "unknown" no matter what state it is actually in — a
    # false alarm stacked on a real one, on the path that is already degraded.
    GOVERNANCE_HEALTH_URL="${GOVERNANCE_HEALTH_URL:-http://localhost:8767/health}"
    if check "governance (fallback)" "$GOVERNANCE_HEALTH_URL" 200; then
        governance_ok=1
    else
        failures=$((failures + 1))
    fi
fi

# --- edge node ----------------------------------------------------------------
# Anima runs on a separate host (e.g. a Pi over Tailscale), so it is deliberately
# not in the governance-host roster. The hardcoded default is KEPT: the live
# com.unitares.health-watchdog.plist sets no EnvironmentVariables, so making this
# opt-in would silently stop monitoring that is working today — the exact failure
# class this script exists to prevent. See the "Deferred" note in
# docs/install/cross-machine-surface.md; removing the default is a follow-up that
# must come AFTER the operator sets the env var, not before.
ANIMA_HEALTH_URL="${ANIMA_HEALTH_URL:-http://localhost:8766/health}"
check "anima" "$ANIMA_HEALTH_URL" 200 10 || failures=$((failures + 1))

# --- PostgreSQL, via the governance health detail -----------------------------
# Gated on the GOVERNANCE probe specifically, not on the aggregate. With six
# rostered services, an aggregate gate would let a routine dialectic-live or
# wave3a restart suppress a real pool problem for the whole window.
if [ "$governance_ok" -eq 1 ]; then
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
