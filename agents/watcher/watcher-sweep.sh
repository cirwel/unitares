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
#                                 (required; no workspace is selected by default)
#   UNITARES_WATCHER_SWEEP_MAX    max files per sweep (default: 40)
#   UNITARES_WATCHER_SWEEP_MAX_WINDOW_H
#                                 oldest edit the sweep will go back for, in
#                                 hours (default: 48). See "Window" below.
#   UNITARES_WATCHER_SWEEP_REVIEW set to 1 to also run the free-form review
#                                 scan on each file (default: off). The sweep
#                                 runs the pattern scan only. Review findings
#                                 (pattern R000) come from no rule, so nothing
#                                 calibrates them, and unattended they arrive
#                                 faster than anyone reads them: one deployment
#                                 collected 370 in six days, one of which was
#                                 ever adjudicated.
#   UNITARES_WATCHER_AGENT        path to agent.py (default: alongside this file)
#   UNITARES_WATCHER_PYTHON       interpreter for agent.py (default: python3 on
#                                 PATH). A scheduler's PATH can resolve a
#                                 python3 without Watcher's dependencies, so set
#                                 this to the interpreter the hooks use.
#
# Each run also performs the findings lifecycle (agent.py --lifecycle) before
# anything else: the stale-path sweep and the line-drift sweep, exactly what
# surface_pending runs before a chime. They keep running whether or not
# anything is displaying findings and whether or not Ollama is up; lifecycle
# needs no model. Compaction (--compact) is deliberately not part of it: it
# deletes resolved rows, which are labels, so it stays an explicit act.
#
# Host schedulers and their selected scan roots belong in deployment config.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCHER_AGENT="${UNITARES_WATCHER_AGENT:-${SCRIPT_DIR}/agent.py}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PY="${UNITARES_WATCHER_PYTHON:-python3}"

ROOTS="${UNITARES_WATCHER_SWEEP_ROOTS:-}"
if [[ -z "$ROOTS" ]]; then
    echo 'watcher-sweep: set UNITARES_WATCHER_SWEEP_ROOTS to explicit scan roots' >&2
    exit 2
fi
MAX_FILES="${UNITARES_WATCHER_SWEEP_MAX:-40}"
MAX_WINDOW_H="${UNITARES_WATCHER_SWEEP_MAX_WINDOW_H:-48}"
case "$MAX_WINDOW_H" in ''|*[!0-9]*) MAX_WINDOW_H=48 ;; esac
SWEEP_REVIEW="${UNITARES_WATCHER_SWEEP_REVIEW:-0}"
MAX_CONCURRENT=3
LOCK_DIR="/tmp/unitares-watcher-locks"
STATE_DIR="$HOME/.unitares"
STATE_FILE="${STATE_DIR}/watcher-sweep.state"
# One marker per file this sweep has scanned since the watermark last moved.
DONE_DIR="${STATE_DIR}/watcher-sweep.done"
LOG_FILE="$HOME/Library/Logs/unitares-watcher.log"

if [[ ! -f "${WATCHER_AGENT}" ]]; then
    exit 0
fi

log() {
    printf '%s [info] sweep: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_FILE"
}

mkdir -p "$STATE_DIR" "$LOCK_DIR"

# --- Findings lifecycle: every run, before any skip below ---
# Runs under the findings lock inside agent.py. A failure is logged and the
# scan still proceeds; lifecycle retries on the next interval.
LIFECYCLE_RC=0
"$PY" "${WATCHER_AGENT}" --lifecycle >/dev/null 2>&1 || LIFECYCLE_RC=$?
if [[ "$LIFECYCLE_RC" -ne 0 ]]; then
    log "lifecycle failed (exit ${LIFECYCLE_RC}); retrying next interval"
fi

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

# The watermark is held whenever a sweep hits its file cap, so that the rest is
# picked up next time. Edits can arrive faster than the cap drains them, and
# then the watermark never moves and the window only grows: one deployment's
# watermark sat 14 days back, with 219,000 files newer than it. So the window
# has a floor. A watermark older than MAX_WINDOW_H is moved up to it, and the
# edits before that are left to the hook, which is the primary trigger.
if [[ "$MAX_WINDOW_H" -gt 0 ]] \
    && [[ -n "$("$FIND" "$STATE_FILE" -mmin "+$(( MAX_WINDOW_H * 60 ))" 2>/dev/null)" ]]; then
    FLOOR_STAMP=$(date -v-"${MAX_WINDOW_H}"H +%Y%m%d%H%M.%S 2>/dev/null \
        || date -d "-${MAX_WINDOW_H} hours" +%Y%m%d%H%M.%S 2>/dev/null || true)
    if [[ -n "$FLOOR_STAMP" ]] && touch -t "$FLOOR_STAMP" "$STATE_FILE" 2>/dev/null; then
        log "watermark was more than ${MAX_WINDOW_H}h old; moved up to ${MAX_WINDOW_H}h, earlier edits are not swept"
    fi
fi

# Stamp the next watermark NOW, before scanning, so edits made *during* the
# sweep are still newer than it and get picked up on the following pass.
PENDING_STATE="${STATE_FILE}.pending"
: > "$PENDING_STATE"

# --- Don't compete with an active editing session ---
# Counts the hook's scans too: both share one model.
RUNNING=$(pgrep -f "agents/watcher/agent.py --(all|file) " 2>/dev/null | wc -l | tr -d ' ')
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

# Keep only real edits. A git checkout (new worktree, branch switch, pull)
# moves the mtime of every file it writes, so mtime alone makes a fresh
# worktree look like a thousand edits and re-flags existing code once per
# copy. sweep_filter keeps a file git reports modified or untracked, or that a
# commit since the watermark touched, and every file outside a work tree. If
# the filter itself fails, scan the unfiltered list rather than skip edits.
RAW_COUNT=$(printf '%s\n' "$CANDIDATES" | grep -c . || true)
if FILTERED=$(printf '%s\n' "$CANDIDATES" | PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m agents.watcher.sweep_filter --since-file "$STATE_FILE" 2>/dev/null); then
    CANDIDATES="$FILTERED"
else
    log "edit filter failed; scanning unfiltered candidates"
fi
FILTERED_COUNT=$(printf '%s\n' "$CANDIDATES" | grep -c . || true)
UNEDITED=$(( RAW_COUNT - FILTERED_COUNT ))

# Count lines; do not test the string. `${CANDIDATES//[[:space:]]/}` is
# quadratic in bash 3.2, which is /bin/bash on macOS: 34 seconds on 100 paths,
# and a sweep with 1,500 candidates sat in that one test for 22 hours.
TOTAL="$FILTERED_COUNT"
if [[ "$TOTAL" -eq 0 ]]; then
    mv -f "$PENDING_STATE" "$STATE_FILE"
    rm -rf "$DONE_DIR"
    exit 0
fi

# One scan of one file (or region). `--file PATH` first, so the concurrency
# count above sees the pattern scan and the review scan alike.
scan_one() {
    local target="$1"
    shift
    if [[ "$SWEEP_REVIEW" == "1" ]]; then
        "$PY" "${WATCHER_AGENT}" --file "$target" --all "$@" >/dev/null 2>&1
    else
        "$PY" "${WATCHER_AGENT}" --file "$target" "$@" >/dev/null 2>&1
    fi
}

SCANNED=0
SKIPPED_HOOK=0
SKIPPED_DONE=0
mkdir -p "$DONE_DIR"
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
    DONE_FILE="${DONE_DIR}/${FILE_HASH}"
    if [[ -f "$LOCK_FILE" ]]; then
        LOCK_MTIME=$(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0)
        FILE_MTIME=$(stat -f %m "$FILE_PATH" 2>/dev/null || echo 0)
        if [[ "$LOCK_MTIME" -ge "$FILE_MTIME" ]]; then
            SKIPPED_HOOK=$(( SKIPPED_HOOK + 1 ))
            touch "$DONE_FILE"
            continue
        fi
    fi

    # --- Did an earlier sweep in this window already scan this edit? ---
    # While the watermark is held, every sweep sees the same candidates in the
    # same order. The hook's lock cannot carry "already scanned" between
    # sweeps: watcher-hook.sh deletes locks older than ten minutes. Without a
    # record of its own, a sweep that hit the cap scanned the same first
    # MAX_FILES files every interval and never reached the rest (one
    # deployment: 64 files scanned five times or more, one of them 45 times).
    if [[ -f "$DONE_FILE" && ! "$FILE_PATH" -nt "$DONE_FILE" ]]; then
        SKIPPED_DONE=$(( SKIPPED_DONE + 1 ))
        continue
    fi

    # Claim it for both triggers before spending the inference.
    touch "$LOCK_FILE"

    # Same region extraction the hook uses, so prompts stay ~60-150 lines.
    REGIONS=$(cd "$REPO_ROOT" && "$PY" -m agents.watcher.hook_input --file "$FILE_PATH" 2>/dev/null || true)
    if [[ -z "$REGIONS" ]]; then
        scan_one "$FILE_PATH"
    else
        while IFS= read -r REGION; do
            [[ -z "$REGION" ]] && continue
            scan_one "$FILE_PATH" --region "$REGION"
        done <<< "$REGIONS"
    fi
    touch "$DONE_FILE"
    SCANNED=$(( SCANNED + 1 ))
done <<< "$CANDIDATES"

# Advance the watermark only after a completed pass. If the cap truncated the
# work, hold the old watermark so the remainder is picked up next sweep; the
# markers in DONE_DIR are what make that sweep start where this one stopped.
# They describe the held window only, so they go when the watermark moves.
if [[ "$SCANNED" -ge "$MAX_FILES" ]]; then
    rm -f "$PENDING_STATE"
    log "${TOTAL} changed, scanned ${SCANNED} (cap ${MAX_FILES} hit — watermark held), ${SKIPPED_HOOK} already covered by hook, ${SKIPPED_DONE} already scanned in this window, ${UNEDITED} unedited checkout files skipped"
else
    mv -f "$PENDING_STATE" "$STATE_FILE"
    rm -rf "$DONE_DIR"
    log "${TOTAL} changed, scanned ${SCANNED}, ${SKIPPED_HOOK} already covered by hook, ${SKIPPED_DONE} already scanned in this window, ${UNEDITED} unedited checkout files skipped"
fi

exit 0
