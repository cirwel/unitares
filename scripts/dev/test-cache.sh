#!/usr/bin/env bash
# test-cache.sh — tree-hash pytest cache
#
# Hashes tracked repo inputs plus untracked files under pytest-relevant paths.
# If tests already passed against this exact input state, prints
# the cached summary and exits 0 without re-running pytest.
#
# Usage:
#   ./scripts/dev/test-cache.sh              # default: pytest tests/ agents/ -q --tb=short -x
#   ./scripts/dev/test-cache.sh --quick      # same gate without coverage instrumentation
#   ./scripts/dev/test-cache.sh --staged     # hash staged commit candidate
#   ./scripts/dev/test-cache.sh --fresh      # ignore cache, force run
#   ./scripts/dev/test-cache.sh -- -k "test_foo"  # extra pytest args after --

set -euo pipefail

# Portable mtime in epoch seconds (macOS `stat -f` is not GNU `stat -c`)
_cache_mtime() {
  python3 -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$1"
}

CACHE_DIR=".test-cache"
CACHE_VERSION="v4"
CACHE_FORMAT="test-cache-result-v1"
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# --- parse args ---
FRESH=false
STAGED=false
QUICK=false
PYTEST_EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh) FRESH=true; shift ;;
        --staged) STAGED=true; shift ;;
        --quick) QUICK=true; shift ;;
        --)      shift; PYTEST_EXTRA=("$@"); break ;;
        *)       PYTEST_EXTRA+=("$1"); shift ;;
    esac
done

TRACKED_HASH_PATHS=(".")
UNTRACKED_HASH_PATHS=(
    "src"
    "tests"
    "agents"
    "governance_core"
    "db"
    "scripts"
    "config"
    "commands"
    "docs"
    "dashboard"
    "elixir"
    "skills"
    "pyproject.toml"
    "requirements*.txt"
    "VERSION"
    "AGENTS.md"
    "CLAUDE.md"
    "CODEX_START.md"
    "README.md"
    "CONTRIBUTING.md"
    "SECURITY.md"
    "Makefile"
    "Dockerfile"
    "docker-compose.yml"
)

_hash_worktree_inputs() {
    python3 - <<'PY'
import hashlib
import subprocess

tracked_proc = subprocess.run(
    ["git", "ls-files", "-z", "--", "."],
    check=True,
    stdout=subprocess.PIPE,
)
untracked_patterns = [
    "src",
    "tests",
    "agents",
    "governance_core",
    "db",
    "scripts",
    "config",
    "commands",
    "docs",
    "dashboard",
    "elixir",
    "skills",
    "pyproject.toml",
    "requirements*.txt",
    "VERSION",
    "AGENTS.md",
    "CLAUDE.md",
    "CODEX_START.md",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
]
untracked_proc = subprocess.run(
    ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", *untracked_patterns],
    check=True,
    stdout=subprocess.PIPE,
)
paths = sorted(
    {
        p.decode("utf-8", "surrogateescape")
        for p in tracked_proc.stdout.split(b"\0") + untracked_proc.stdout.split(b"\0")
        if p
    }
)
h = hashlib.sha256()
h.update(b"worktree-inputs-v2\0")
for path in paths:
    h.update(path.encode("utf-8", "surrogateescape"))
    h.update(b"\0")
    try:
        with open(path, "rb") as fh:
            h.update(fh.read())
    except FileNotFoundError:
        h.update(b"<deleted>")
    h.update(b"\0")
print(h.hexdigest())
PY
}

_hash_staged_inputs() {
    python3 - <<'PY'
import hashlib
import subprocess

proc = subprocess.run(
    ["git", "ls-files", "-s", "-z", "--", "."],
    check=True,
    stdout=subprocess.PIPE,
)
records = sorted(r for r in proc.stdout.split(b"\0") if r)
h = hashlib.sha256()
h.update(b"staged-inputs-v2\0")
for record in records:
    h.update(record)
    h.update(b"\0")
print(h.hexdigest())
PY
}

_hash_runtime() {
    "$PYTHON" - <<'PY'
import hashlib
import importlib.metadata
import os
import platform
import sys

packages = [
    "pytest",
    "pytest-cov",
    "pytest-asyncio",
    "hypothesis",
]
env_names = [
    "PYTEST_ADDOPTS",
    "STRICT_IDENTITY_REQUIRED",
    "UNITARES_KNOWLEDGE_BACKEND",
    "UNITARES_DB_URL",
    "DATABASE_URL",
    "REDIS_URL",
]

h = hashlib.sha256()
h.update(b"runtime-v1\0")
h.update(sys.executable.encode("utf-8", "surrogateescape"))
h.update(b"\0")
h.update(platform.python_version().encode("utf-8"))
h.update(b"\0")
for package in packages:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = "<missing>"
    h.update(f"{package}={version}".encode("utf-8"))
    h.update(b"\0")
for name in env_names:
    h.update(f"{name}={os.environ.get(name, '')}".encode("utf-8", "surrogateescape"))
    h.update(b"\0")
print(h.hexdigest())
PY
}

_hash_pytest_args() {
    python3 - "$@" <<'PY'
import hashlib
import sys

h = hashlib.sha256()
for arg in sys.argv[1:]:
    h.update(arg.encode("utf-8", "surrogateescape"))
    h.update(b"\0")
print(h.hexdigest())
PY
}

_print_staged_dirty_inputs() {
    git diff --name-only -- "${TRACKED_HASH_PATHS[@]}"
    git ls-files --others --exclude-standard -- "${UNTRACKED_HASH_PATHS[@]}"
}

PYTHON="${UNITARES_PYTHON:-python3}"
RUNTIME_HASH=$(_hash_runtime)

# --- compute tree hash ---
HASH_MODE="worktree"
if [[ "$STAGED" == true ]]; then
    HASH_MODE="staged"
    DIRTY_INPUTS=$(_print_staged_dirty_inputs)
    if [[ -n "$DIRTY_INPUTS" ]]; then
        echo "[test-cache] --staged refused: unstaged or untracked files would affect pytest:" >&2
        echo "$DIRTY_INPUTS" >&2
        echo "[test-cache] stash them, stage them, or use a clean worktree before validating the staged tree." >&2
        exit 4
    fi
    TREE_HASH=$(_hash_staged_inputs)
else
    TREE_HASH=$(_hash_worktree_inputs)
fi

TEST_PROFILE="coverage"
if [[ "$QUICK" == true ]]; then
    TEST_PROFILE="quick"
fi

PYTEST_ARGS_HASH=$(_hash_pytest_args ${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"})
CACHE_KEY=$(printf '%s\0%s\0%s\0%s\0%s\0%s\0' "$CACHE_VERSION" "$HASH_MODE" "$TEST_PROFILE" "$TREE_HASH" "$PYTEST_ARGS_HASH" "$RUNTIME_HASH" | shasum -a 256 | cut -d' ' -f1)
CACHE_FILE="$CACHE_DIR/$CACHE_KEY"
CACHE_LABEL="$HASH_MODE inputs $TREE_HASH profile $TEST_PROFILE runtime $RUNTIME_HASH"
if [[ ${#PYTEST_EXTRA[@]} -gt 0 ]]; then
    CACHE_LABEL="$CACHE_LABEL args $PYTEST_ARGS_HASH"
fi

_valid_cache_file() {
    [[ -f "$1" ]] || return 1
    local first_line=""
    IFS= read -r first_line < "$1" || true
    [[ "$first_line" == "$CACHE_FORMAT" ]]
}

_print_cache_file() {
    { IFS= read -r _format_line || true; cat; } < "$1"
}

# --- cache hit (fast path, no lock) ---
if [[ "$FRESH" == false ]] && _valid_cache_file "$CACHE_FILE"; then
    AGE_SECS=$(( $(date +%s) - $(_cache_mtime "$CACHE_FILE") ))
    AGE_MIN=$(( AGE_SECS / 60 ))
    echo "[test-cache] HIT — $CACHE_LABEL (cached ${AGE_MIN}m ago)"
    _print_cache_file "$CACHE_FILE"
    exit 0
fi

# --- acquire cross-invocation lock before running pytest ---
#
# Without this, two concurrent test-cache.sh callers (pre-commit hook
# firing while an agent's auto-test hook has already started a run, or
# two agents hitting the script from different sessions) both enter the
# miss path and spawn parallel pytests that hammer Postgres/Redis and
# leave ghost/zombie children. macOS has no native flock(1); use atomic
# mkdir as the lock primitive and record the holder PID so stale locks
# from killed holders can be reclaimed.
LOCK_DIR="${UNITARES_TEST_CACHE_LOCK_DIR:-/tmp/unitares-test-cache.lock}"
LOCK_HOLDER="$LOCK_DIR/holder.pid"
LOCK_WAIT_MAX=600   # seconds
LOCK_WAITED=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    HOLDER_PID="$(cat "$LOCK_HOLDER" 2>/dev/null || echo "")"
    if [[ -n "$HOLDER_PID" ]] && ! kill -0 "$HOLDER_PID" 2>/dev/null; then
        echo "[test-cache] reclaiming stale lock from dead pid $HOLDER_PID"
        rm -rf "$LOCK_DIR"
        continue
    fi
    if [[ "$LOCK_WAITED" -eq 0 ]]; then
        echo "[test-cache] waiting for pytest lock (held by pid ${HOLDER_PID:-?})..."
    fi
    sleep 2
    LOCK_WAITED=$(( LOCK_WAITED + 2 ))
    if [[ "$LOCK_WAITED" -ge "$LOCK_WAIT_MAX" ]]; then
        echo "[test-cache] gave up waiting for lock after ${LOCK_WAIT_MAX}s — exiting 3" >&2
        exit 3
    fi
done
echo "$$" > "$LOCK_HOLDER"
TMPOUT=""
PYTEST_PID=""
CACHE_TMP=""

_cleanup_test_cache() {
    if [[ -n "${TMPOUT:-}" ]]; then
        rm -f "$TMPOUT"
    fi
    if [[ -n "${CACHE_TMP:-}" ]]; then
        rm -f "$CACHE_TMP"
    fi
    rm -rf "$LOCK_DIR"
}

_interrupt_test_cache() {
    local signal_name="$1"
    local exit_code="$2"
    trap - INT TERM
    if [[ -n "${PYTEST_PID:-}" ]] && kill -0 "$PYTEST_PID" 2>/dev/null; then
        kill -s "$signal_name" "$PYTEST_PID" 2>/dev/null || true
        wait "$PYTEST_PID" 2>/dev/null || true
    fi
    echo "[test-cache] INTERRUPTED ($signal_name) — not cached" >&2
    exit "$exit_code"
}

trap '_cleanup_test_cache' EXIT
trap '_interrupt_test_cache INT 130' INT
trap '_interrupt_test_cache TERM 143' TERM

# --- double-check cache now that we hold the lock ---
# The holder ahead of us may have just populated the cache for this
# tree hash; skip pytest if so.
if [[ "$FRESH" == false ]] && _valid_cache_file "$CACHE_FILE"; then
    AGE_SECS=$(( $(date +%s) - $(_cache_mtime "$CACHE_FILE") ))
    AGE_MIN=$(( AGE_SECS / 60 ))
    echo "[test-cache] HIT (post-lock) — $CACHE_LABEL (cached ${AGE_MIN}m ago)"
    _print_cache_file "$CACHE_FILE"
    exit 0
fi

# --- cache miss: run pytest ---
mkdir -p "$CACHE_DIR"
echo "[test-cache] MISS — $CACHE_LABEL, running pytest..."

if [[ "$QUICK" == true ]]; then
    PYTEST_CMD=("$PYTHON" -m pytest tests/ agents/ -q --tb=short -x \
        ${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"})
else
    PYTEST_CMD=("$PYTHON" -m pytest tests/ agents/ -q --tb=short -x \
        --cov=src --cov=agents/sdk/src/unitares_sdk --cov=agents \
        --cov-report=term-missing --cov-fail-under=75 \
        ${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"})
fi
TMPOUT=$(mktemp)
set +e
python3 - "$TMPOUT" "${PYTEST_CMD[@]}" <<'PY' &
import os
import signal
import subprocess
import sys
import time

output_path, *command = sys.argv[1:]
pytest_process = None
pending_signal = None


def descendant_pids(root_pid: int) -> set[int]:
    """Snapshot recursive descendants, including new-session children."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        check=True,
        capture_output=True,
        text=True,
    )
    children: dict[int, set[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            pid, parent = int(pid_text), int(parent_text)
        except (ValueError, TypeError):
            continue
        children.setdefault(parent, set()).add(pid)

    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def safe_descendant_pids(root_pid: int) -> set[int]:
    """Return an empty snapshot when platform process inspection fails."""
    try:
        return descendant_pids(root_pid)
    except (OSError, subprocess.SubprocessError):
        return set()


def process_exists(pid: int) -> bool:
    """Return whether a captured process still exists or is inaccessible."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_processes(signum: int, pids: set[int]) -> None:
    """Signal the pytest process group plus detached descendants."""
    assert pytest_process is not None
    try:
        os.killpg(pytest_process.pid, signum)
    except (PermissionError, ProcessLookupError):
        pass
    for pid in pids:
        try:
            os.kill(pid, signum)
        except (PermissionError, ProcessLookupError):
            pass


def interrupt(signum: int, _frame: object) -> None:
    """Bound teardown before reporting the signal exit to the shell wrapper."""
    global pending_signal
    if pytest_process is None:
        pending_signal = signum
        return

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descendants = safe_descendant_pids(pytest_process.pid)
    signal_processes(signum, descendants)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if pytest_process.poll() is None:
            current_descendants = safe_descendant_pids(pytest_process.pid)
            new_descendants = current_descendants - descendants
            if new_descendants:
                descendants.update(new_descendants)
                signal_processes(signum, new_descendants)
        survivors = {pid for pid in descendants if process_exists(pid)}
        if pytest_process.poll() is not None and not survivors:
            break
        time.sleep(0.05)

    survivors = {pid for pid in descendants if process_exists(pid)}
    if pytest_process.poll() is None or survivors:
        signal_processes(signal.SIGKILL, survivors)
    try:
        pytest_process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        late_descendants = safe_descendant_pids(pytest_process.pid)
        signal_processes(signal.SIGKILL, descendants | late_descendants)
        pytest_process.wait()
    raise SystemExit(128 + signum)


# Install handlers before spawning pytest. A signal delivered during Popen is
# recorded and handled as soon as the child PID is available.
signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGTERM, interrupt)
pytest_process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
if pending_signal is not None:
    interrupt(pending_signal, None)

assert pytest_process.stdout is not None
read_chunk = getattr(pytest_process.stdout, "read1", pytest_process.stdout.read)
with open(output_path, "wb") as output:
    while chunk := read_chunk(65536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        output.write(chunk)
        output.flush()

raise SystemExit(pytest_process.wait())
PY
PYTEST_PID=$!
wait "$PYTEST_PID"
EXIT_CODE=$?
PYTEST_PID=""
set -e

if [[ $EXIT_CODE -eq 0 ]]; then
    # Publish only a complete, versioned passing result. A signal during `tail`
    # removes CACHE_TMP in the EXIT trap; readers never see a partial entry.
    CACHE_TMP=$(mktemp "$CACHE_DIR/.${CACHE_KEY}.tmp.XXXXXX")
    {
        printf '%s\n' "$CACHE_FORMAT"
        tail -5 "$TMPOUT"
    } > "$CACHE_TMP"
    mv -f "$CACHE_TMP" "$CACHE_FILE"
    CACHE_TMP=""
    echo "[test-cache] CACHED — $CACHE_LABEL"
else
    echo "[test-cache] FAILED (exit $EXIT_CODE) — not cached"
fi

# prune old entries (keep last 20)
ENTRIES=$(ls -t "$CACHE_DIR"/ 2>/dev/null | tail -n +21)
if [[ -n "$ENTRIES" ]]; then
    echo "$ENTRIES" | while read -r f; do rm -f "$CACHE_DIR/$f"; done
fi

exit "$EXIT_CODE"
