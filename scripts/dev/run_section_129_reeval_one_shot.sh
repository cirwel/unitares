#!/usr/bin/env bash
# One-shot wrapper for the Wave 1 §129 re-evaluation. Fires at 09:00 MDT on
# 2026-06-02 (T+14 of the 2026-05-19 anchor) via
# `com.unitares.wave-1-section-129-reeval` launchd plist.
#
# Self-unloads after firing so it doesn't run again on 2027-06-02. The
# script itself is idempotent against the 2026-05-19 anchor, so a stray
# re-fire would only re-confirm the same window — but the cleaner UX is to
# tear down after one shot.

set -u

LABEL="com.unitares.wave-1-section-129-reeval"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
PROJECT_ROOT="/Users/cirwel/projects/unitares"
LOG_DIR="${PROJECT_ROOT}/data/logs"
LOG_FILE="${LOG_DIR}/section_129_reeval_2026-06-02.log"

mkdir -p "$LOG_DIR"

{
    printf '=== Wave 1 §129 re-evaluation — fired %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    cd "$PROJECT_ROOT" || exit 2
    /usr/bin/env python3 scripts/dev/section_129_reeval.py
    RC=$?
    printf '\n=== exit code: %d ===\n' "$RC"
    case "$RC" in
        0) printf 'verdict: SUBSTANTIVE PASS (all three conditions met)\n' ;;
 1) printf 'verdict: FAIL — at least one condition not met. Substrate-question evidence; AMENDMENT 2026-05-04.\n' ;;
        2) printf 'verdict: PENDING (window incomplete — this should not happen on T+14)\n' ;;
        *) printf 'verdict: ERROR (unexpected exit code)\n' ;;
    esac
} >> "$LOG_FILE" 2>&1

# Self-unload via the same launchctl handle that fired this wrapper. The
# `bootout` form works on macOS 13+; falls back to legacy `unload` if not.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null \
    || true

# Note: deliberately leaving the .plist file on disk for audit trail.
# Re-arming for a future re-eval is a `launchctl bootstrap gui/$(id -u) <plist>`
# away with an updated StartCalendarInterval.
