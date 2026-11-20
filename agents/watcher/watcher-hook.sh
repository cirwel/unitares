#!/bin/bash
# Watcher PostToolUse hook
#
# Fires the UNITARES Watcher agent in the background when a Python/JS/TS/etc.
# file is edited or written. Returns immediately so the editor never blocks
# on the model call.
#
# Debounce: skips if the same file was scanned within DEBOUNCE_SECS.
# Concurrency cap: skips if MAX_CONCURRENT watcher processes are already running.
# Both prevent Ollama queue saturation during rapid editing sessions.
#
# Wire into ~/.claude/settings.json under hooks.PostToolUse with matcher
# "Edit|Write" alongside any existing hooks.

set -u

DEBOUNCE_SECS=30
MAX_CONCURRENT=3
LOCK_DIR="${UNITARES_WATCHER_LOCK_DIR:-/tmp/unitares-watcher-locks}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCHER_AGENT="${UNITARES_WATCHER_AGENT:-${SCRIPT_DIR}/agent.py}"

if [[ ! -f "${WATCHER_AGENT}" ]]; then
    exit 0
fi

INPUT=$(cat)
FILE_PATH=$(python3 - "$INPUT" <<'PY' 2>/dev/null || true
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    data = json.loads(raw) if raw else {}
except Exception:
    data = {}

tool_input = data.get("tool_input", data.get("input", data))
file_path = tool_input.get("file_path") or tool_input.get("path") or ""
print(file_path)
PY
)

# Bail fast if no file path
if [[ -z "$FILE_PATH" ]]; then
    exit 0
fi

# Only scan source code files. The watcher itself has additional skip rules
# but doing the cheap extension filter here avoids spawning a python process
# for every markdown / json edit.
case "$FILE_PATH" in
    *.py|*.pyi|*.js|*.jsx|*.ts|*.tsx|*.go|*.rs|*.rb|*.java|*.kt|*.swift|*.c|*.cc|*.cpp|*.h|*.hpp|*.cs|*.php|*.lua|*.sh|*.bash|*.zsh)
        ;;
    *)
        exit 0
        ;;
esac

# --- Concurrency cap ---
# Count running watcher agent.py processes. If at the limit, skip.
# Same matcher as watcher-sweep.sh, whose pattern-only scans start with
# `--file`: both triggers share one model, so each must count the other's.
RUNNING=$(pgrep -f "agents/watcher/agent.py --(all|file) " 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RUNNING" -ge "$MAX_CONCURRENT" ]]; then
    exit 0
fi

# --- Per-file debounce ---
# Hash the file path to create a stable lock filename.
mkdir -p "$LOCK_DIR"
FILE_HASH=$(printf '%s' "$FILE_PATH" | shasum -a 256 | cut -c1-16)
LOCK_FILE="${LOCK_DIR}/${FILE_HASH}.lock"

if [[ -f "$LOCK_FILE" ]]; then
    # macOS stat -f %m gives mtime as epoch seconds
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE") ))
    if [[ "$LOCK_AGE" -lt "$DEBOUNCE_SECS" ]]; then
        exit 0
    fi
fi
touch "$LOCK_FILE"

# --- Stale lock cleanup (opportunistic, non-blocking) ---
find "$LOCK_DIR" -name "*.lock" -mmin +10 -delete 2>/dev/null &

# --- Region extraction via git diff ---
# Ask the hook_input module for the changed regions so the watcher sees
# ~60-150 lines (one hunk + 5 context on each side) instead of the whole
# file. Empty output = file untracked, clean, or git unavailable; fall
# back to scanning head. Bounded by its own internal 5s timeout.
REGIONS=$(python3 -m agents.watcher.hook_input --file "$FILE_PATH" 2>/dev/null || true)

# Fire and forget: detach a subshell that runs the watcher(s) serially.
# One scan per disjoint region cluster keeps each prompt small; running
# them in sequence (within one background spawn) avoids saturating the
# Ollama queue. stdin/stdout/stderr go to /dev/null so the editor never
# sees the model call.
(
    if [[ -z "$REGIONS" ]]; then
        python3 "${WATCHER_AGENT}" --all --file "$FILE_PATH"
    else
        while IFS= read -r REGION; do
            [[ -z "$REGION" ]] && continue
            python3 "${WATCHER_AGENT}" --all --file "$FILE_PATH" --region "$REGION"
        done <<< "$REGIONS"
    fi
) </dev/null >/dev/null 2>&1 &

# Disown so the watcher survives the hook process exit
disown 2>/dev/null || true

exit 0
