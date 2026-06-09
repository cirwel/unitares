"""Leaf utilities shared by agent.py and findings.py.

Split out so findings.py can depend on log/path helpers without pulling
agent.py (which would create a circular import, since agent.py imports
from findings.py).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

LOG_FILE = Path.home() / "Library" / "Logs" / "unitares-watcher.log"

# Legacy state location — relative to whichever checkout this module loads from.
# Kept only as a migration source; nothing should write here anymore.
_LEGACY_STATE_DIR = PROJECT_ROOT / "data" / "watcher"

# Files that make up Watcher's local state, migrated together.
_STATE_FILES = ("findings.jsonl", "dedup.json", "pattern_floor.json")

_state_dir_cache: Path | None = None
_legacy_migration_done = False


def watcher_state_dir() -> Path:
    """Checkout-independent home for Watcher's local state.

    The Watcher agent (writer) is pinned to the dev checkout by the PostToolUse
    hook, while http_api (reader) runs from whichever checkout serves the live
    MCP — after the deploy-worktree cutover those are different trees. Because
    ``data/watcher`` is gitignored local state, the deploy tree had no such dir
    and the dashboard panel silently read zeroes. Anchoring state under
    ``~/.unitares`` (same place as the identity anchor) makes writer and reader
    agree regardless of which checkout each runs from.

    Override with ``UNITARES_WATCHER_DATA_DIR``. Pure path resolution with no
    filesystem side effects — call :func:`migrate_legacy_watcher_state` once at
    process start to carry forward any pre-existing legacy state.
    """
    global _state_dir_cache
    if _state_dir_cache is not None:
        return _state_dir_cache

    override = os.environ.get("UNITARES_WATCHER_DATA_DIR")
    _state_dir_cache = (
        Path(override).expanduser()
        if override
        else Path.home() / ".unitares" / "watcher"
    )
    return _state_dir_cache


def migrate_legacy_watcher_state() -> None:
    """Copy legacy checkout-relative state into the shared dir if absent there.

    Idempotent and best-effort: runs its filesystem work once per process, and
    a file is only copied when it exists in the legacy dir and not yet in the
    target. The legacy copy is left untouched so an older-code Watcher still
    running mid-rollout is never disrupted. Kept out of :func:`watcher_state_dir`
    so importing the path constants never touches the filesystem.

    Mid-rollout note: because this copies a one-time snapshot and never re-copies,
    a still-running old-code Watcher that keeps appending to the legacy dir will
    not have those appends reflected in the shared dir until it restarts onto the
    new code. The reader (``http_api._watcher_findings_path``) compensates by
    falling back to the legacy file while the shared one is empty, so the panel
    shows live data rather than a frozen snapshot during the window.
    """
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    target = watcher_state_dir()
    legacy = _LEGACY_STATE_DIR
    try:
        if legacy.resolve() == target.resolve() or not legacy.is_dir():
            return
    except OSError:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    import shutil

    for name in _STATE_FILES:
        src = legacy / name
        dst = target / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                log(f"migrated watcher state {name} from {legacy} to {target}")
            except OSError as e:
                log(f"watcher state migration failed for {name}: {e}", "warning")

# Cap for ~/Library/Logs/unitares-watcher.log rotation. Watcher logs a few
# lines per scan; 5000 lines ≈ 500 scans of operational history, which is
# plenty for debugging. Without this, the log file was a direct P002 match
# against the Watcher's own pattern library — unbounded append forever.
MAX_LOG_LINES = 5000


def log(msg: str, level: str = "info") -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} [{level}] {msg}\n"
    try:
        with LOG_FILE.open("a") as f:
            f.write(line)
    except OSError:
        pass  # never let logging errors take down the watcher
    if os.environ.get("WATCHER_DEBUG") == "1":
        sys.stderr.write(line)


_REPO_ROOT_CACHE: dict[str, str] = {}


def repo_relative_path(file_path: str) -> str:
    """Return ``file_path`` relative to its containing git worktree root.

    Falls back to the absolute string if the path is not inside a git
    repository or git invocation fails. Result is normalized to forward
    slashes so the fingerprint is platform-stable.

    Cached per-directory because hook-driven scans hit the same worktree
    over and over and ``git rev-parse`` is otherwise tens of ms each call.
    """
    if not file_path:
        return file_path
    p = Path(file_path)
    parent_key = str(p.parent if p.is_absolute() else p.resolve().parent)
    toplevel = _REPO_ROOT_CACHE.get(parent_key)
    if toplevel is None:
        try:
            result = subprocess.run(
                ["git", "-C", parent_key, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            toplevel = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            toplevel = ""
        _REPO_ROOT_CACHE[parent_key] = toplevel
    if not toplevel:
        return file_path
    try:
        rel = Path(file_path).resolve().relative_to(Path(toplevel).resolve())
    except ValueError:
        return file_path
    return rel.as_posix()


def hash_line_content(source_line: str | None) -> str:
    """Stable hash of a source line for content-aware fingerprinting.

    Whitespace is stripped from both ends so indent-only reformats do not
    trigger spurious re-flags. Internal whitespace is preserved because it
    can be semantically meaningful (e.g. dict literal formatting).
    """
    normalized = (source_line or "").strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]
