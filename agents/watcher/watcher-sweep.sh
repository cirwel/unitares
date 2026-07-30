#!/bin/bash
# Watcher periodic sweep — session-independent trigger
#
# Watcher's primary trigger is the Claude Code PostToolUse hook
# (watcher-hook.sh). That makes its coverage subscription-gated: no editor
# session means no scans, even though Watcher's inference is entirely local
# (Ollama). This sweep is the backstop — a launchd job that finds source files
# edited since the last sweep and scans the ones the hook did not already
# cover, so Watcher keeps working when nothing is driving the editor.
#
# It is deliberately a backstop, not a duplicate:
#   - Only files with mtime newer than the last successful sweep are candidates.
#   - A candidate is skipped if watcher-hook.sh already scanned that edit — the
#     hook's per-file debounce lock is newer than the file's mtime.
#   - After scanning, the same lock is touched, so the hook debounces too and
#     the two triggers never double-spend inference on one edit.
#
# Config (env):
#   UNITARES_WATCHER_SWEEP_ROOTS  colon-separated roots to walk
#                                 (default: $HOME/projects)
#   UNITARES_WATCHER_SWEEP_MAX    max files per sweep (default: 40)
#   UNITARES_WATCHER_AGENT        path to agent.py (default: alongside this file)
#
# Install: see scripts/ops/com.unitares.watcher-sweep.plist.template

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCHER_AGENT="${UNITARES_WATCHER_AGENT:-${SCRIPT_DIR}/agent.py}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ROOTS="${UNITARES_WATCHER_SWEEP_ROOTS:-$HOME/projects}"
MAX_FILES="${UNITARES_WATCHER_SWEEP_MAX:-40}"
MAX_CONCURRENT=3
LOCK_DIR="/tmp/unitares-watcher-locks"
STATE_DIR="$HOME/.unitares"
STATE_FILE="${STATE_DIR}/watcher-sweep.state"
LOG_FILE="$HOME/Library/Logs/unitares-watcher.log"

if [[ ! -f "${WATCHER_AGENT}" ]]; then
    exit 0
fi

log() {
    printf '%s [info] sweep: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_FILE"
}

mkdir -p "$STATE_DIR" "$LOCK_DIR"

# --- Window: everything modified since the last successful sweep ---
# The watermark is the state file's own mtime, compared with `find -newer`.
# Do NOT switch this to `-newermt "@<epoch>"`: that is a GNU-find extension.
# BSD find — which is what launchd runs as /usr/bin/find — answers
# "Can't parse date/time: @1785363607" and exits 1, which silently yields an
# empty candidate list and a sweep that appears to work while scanning nothing.
# (An interactive shell may have a GNU-compatible `find` shadowing the system
# one, so this does not reproduce by hand.)
#
# First run has no state file. Seed the window at 1 hour rather than scanning
# every source file ever written.
FIND=/usr/bin/find
if [[ ! -f "$STATE_FILE" ]]; then
    touch -t "$(date -v-1H +%Y%m%d%H%M.%S)" "$STATE_FILE" 2>/dev/null || touch "$STATE_FILE"
    log "no watermark — seeding window at 1h"
fi

# Stamp the next watermark NOW, before scanning, so edits made *during* the
# sweep are still newer than it and get picked up on the following pass.
PENDING_STATE="${STATE_FILE}.pending"
: > "$PENDING_STATE"

# --- Don't compete with an active editing session ---
RUNNING=$(pgrep -f "agents/watcher/agent.py --all" 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RUNNING" -ge "$MAX_CONCURRENT" ]]; then
    log "skip — ${RUNNING} scans already running"
    rm -f "$PENDING_STATE"
    exit 0
fi

# --- Ollama reachable? A sweep with no backend just writes 404s to the log. ---
if ! curl -s -o /dev/null -m 3 "http://localhost:11434/api/tags" 2>/dev/null; then
    log "skip — Ollama unreachable at localhost:11434"
    rm -f "$PENDING_STATE"
    exit 0
fi

# --- Candidate files ---
# -newermt takes a timestamp; BSD find accepts @epoch.
CANDIDATES=""
IFS=':' read -r -a ROOT_LIST <<< "$ROOTS"
for ROOT in "${ROOT_LIST[@]}"; do
    [[ -d "$ROOT" ]] || continue
    FOUND=$("$FIND" "$ROOT" \
        \( -name .git -o -name node_modules -o -name .venv -o -name venv \
           -o -name __pycache__ -o -name site-packages -o -name _build \
           -o -name build -o -name dist -o -name .mypy_cache \
           -o -name .pytest_cache -o -name _wt \) -prune -o \
        -type f -newer "$STATE_FILE" \
        \( -name '*.py' -o -name '*.pyi' -o -name '*.js' -o -name '*.jsx' \
           -o -name '*.ts' -o -name '*.tsx' -o -name '*.go' -o -name '*.rs' \
           -o -name '*.rb' -o -name '*.java' -o -name '*.kt' -o -name '*.swift' \
           -o -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.h' \
           -o -name '*.hpp' -o -name '*.cs' -o -name '*.php' -o -name '*.lua' \
           -o -name '*.ex' -o -name '*.exs' -o -name '*.sh' -o -name '*.bash' \
           -o -name '*.zsh' \) \
        -print 2>/dev/null)
    if [[ -n "$FOUND" ]]; then
        CANDIDATES="${CANDIDATES}${FOUND}"$'\n'
    fi
done

# Watcher drops test files internally; filter here to avoid spawning python for them.
CANDIDATES=$(printf '%s' "$CANDIDATES" | grep -v '/tests\?/' | grep -v -E '/(test_[^/]+|[^/]+_test)\.[a-z]+$' || true)

if [[ -z "${CANDIDATES//[[:space:]]/}" ]]; then
    mv -f "$PENDING_STATE" "$STATE_FILE"
    exit 0
fi

TOTAL=$(printf '%s\n' "$CANDIDATES" | grep -c . || true)

SCANNED=0
SKIPPED_HOOK=0
while IFS= read -r FILE_PATH; do
    [[ -z "$FILE_PATH" ]] && continue
    [[ -f "$FILE_PATH" ]] || continue
    if [[ "$SCANNED" -ge "$MAX_FILES" ]]; then
        break
    fi

    # --- Did the hook already cover this edit? ---
    # Lock mtime newer than the file's mtime means watcher-hook.sh fired after
    # the last write. Nothing new to see.
    FILE_HASH=$(printf '%s' "$FILE_PATH" | shasum -a 256 | cut -c1-16)
    LOCK_FILE="${LOCK_DIR}/${FILE_HASH}.lock"
    if [[ -f "$LOCK_FILE" ]]; then
        LOCK_MTIME=$(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0)
        FILE_MTIME=$(stat -f %m "$FILE_PATH" 2>/dev/null || echo 0)
        if [[ "$LOCK_MTIME" -ge "$FILE_MTIME" ]]; then
            SKIPPED_HOOK=$(( SKIPPED_HOOK + 1 ))
            continue
        fi
    fi

    # Claim it for both triggers before spending the inference.
    touch "$LOCK_FILE"

    # Same region extraction the hook uses, so prompts stay ~60-150 lines.
    REGIONS=$(cd "$REPO_ROOT" && python3 -m agents.watcher.hook_input --file "$FILE_PATH" 2>/dev/null || true)
    if [[ -z "$REGIONS" ]]; then
        python3 "${WATCHER_AGENT}" --all --file "$FILE_PATH" >/dev/null 2>&1
    else
        while IFS= read -r REGION; do
            [[ -z "$REGION" ]] && continue
            python3 "${WATCHER_AGENT}" --all --file "$FILE_PATH" --region "$REGION" >/dev/null 2>&1
        done <<< "$REGIONS"
    fi
    SCANNED=$(( SCANNED + 1 ))
done <<< "$CANDIDATES"

# Advance the watermark only after a completed pass. If the cap truncated the
# work, hold the old watermark so the remainder is picked up next sweep.
if [[ "$SCANNED" -ge "$MAX_FILES" ]]; then
    rm -f "$PENDING_STATE"
    log "${TOTAL} changed, scanned ${SCANNED} (cap ${MAX_FILES} hit — watermark held), ${SKIPPED_HOOK} already covered by hook"
else
    mv -f "$PENDING_STATE" "$STATE_FILE"
    log "${TOTAL} changed, scanned ${SCANNED}, ${SKIPPED_HOOK} already covered by hook"
fi

exit 0
