"""
Stale Lock Cleanup Utility

Removes lock files that no process holds. Held-ness (a non-blocking flock) is
the only staleness test: a held lock is never removed, whatever its recorded pid
or age says. Free lock files block nobody, so this is housekeeping; a lock held
by a stuck process is released only when that process exits.
"""

import fcntl
import os
import json
import time
from pathlib import Path
from typing import Dict, Tuple

from src.state_locking import remove_lock_file_if_free

# Import structured logging
from src.logging_utils import get_logger
logger = get_logger(__name__)

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running"""
    if not PSUTIL_AVAILABLE:
        # Fallback: try to send signal 0 (doesn't kill, just checks)
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _recorded_pid(lock_file: Path):
    """The pid the holder wrote into the lock file, for reporting only.

    StateLockManager writes JSON with a "pid" key; the DistributedLock file
    fallback writes a bare integer. None when neither parses, including a
    file the holder has just truncated before rewriting.
    """
    try:
        data = json.loads(lock_file.read_text() or "null")
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("pid")
    return data if isinstance(data, int) and not isinstance(data, bool) else None


def _held_reason(lock_file: Path) -> str:
    """Describe a held lock. The recorded pid is reported, never trusted."""
    pid = _recorded_pid(lock_file)
    if pid is None:
        return "held by a live process (holder pid unknown)"
    alive = "alive" if is_process_alive(pid) else "not running"
    return f"held by a live process (recorded pid {pid}, {alive})"


def check_lock_staleness(lock_file: Path, max_age_seconds: float = 300.0) -> Tuple[bool, str]:
    """
    Report whether a lock file could be removed, without removing it.

    A lock file is stale only when no process holds it (a non-blocking
    exclusive flock succeeds) and it was last touched at least
    ``max_age_seconds`` ago. The recorded pid and timestamp are reported but
    never make a held lock stale; see state_locking.remove_lock_file_if_free.

    Args:
        lock_file: Path to lock file
        max_age_seconds: Minimum age before a free lock file counts as stale

    Returns:
        (is_stale, reason) tuple
    """
    try:
        fd = os.open(str(lock_file), os.O_RDWR)  # never create
    except FileNotFoundError:
        return False, "lock file doesn't exist"
    except OSError as exc:
        return False, f"cannot open lock file: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False, _held_reason(lock_file)
        age = time.time() - os.fstat(fd).st_mtime
        if age < max_age_seconds:
            return False, f"free, but touched {age:.0f}s ago (< {max_age_seconds:.0f}s)"
        return True, "not held by any process"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def cleanup_stale_locks(lock_dir: Path, max_age_seconds: float = 300.0, dry_run: bool = False) -> Dict[str, any]:
    """
    Clean up stale lock files.
    
    Args:
        lock_dir: Directory containing lock files
        max_age_seconds: Minimum age before a free lock file is removed (default: 5 minutes)
        dry_run: If True, only report what would be cleaned, don't actually delete
    
    Returns:
        Dict with cleanup statistics
    """
    if not lock_dir.exists():
        return {
            "cleaned": 0,
            "kept": 0,
            "errors": 0,
            "details": []
        }
    
    lock_files = list(lock_dir.glob("*.lock"))
    cleaned = []
    kept = []
    errors = []
    
    for lock_file in lock_files:
        try:
            if dry_run:
                is_stale, reason = check_lock_staleness(lock_file, max_age_seconds)
            else:
                # Probe and unlink under one held flock; see
                # remove_lock_file_if_free for why this must not be split.
                is_stale, reason = remove_lock_file_if_free(lock_file, max_age_seconds)
                if reason.startswith("held"):
                    reason = _held_reason(lock_file)

            if is_stale:
                cleaned.append({
                    "lock_file": str(lock_file.name),
                    "reason": reason
                })
            else:
                kept.append({
                    "lock_file": str(lock_file.name),
                    "reason": reason
                })
        except Exception as e:
            errors.append({
                "lock_file": str(lock_file.name),
                "error": str(e)
            })
    
    return {
        "cleaned": len(cleaned),
        "kept": len(kept),
        "errors": len(errors),
        "cleaned_locks": cleaned,
        "kept_locks": kept,
        # A separate key: "errors" used to be set twice, the list silently
        # replacing the count.
        "error_locks": errors,
        "dry_run": dry_run
    }


def cleanup_stale_state_locks(project_root: Path = None, max_age_seconds: float = 300.0, dry_run: bool = False) -> Dict[str, any]:
    """
    Clean up stale state lock files (convenience wrapper).
    
    Args:
        project_root: Project root directory. When omitted, sweep the directory
            StateLockManager writes to (honours UNITARES_LOCK_DIR).
        max_age_seconds: Minimum age before a free lock file is removed; held
            locks are never removed
        dry_run: If True, only report what would be cleaned
    
    Returns:
        Dict with cleanup statistics
    """
    if project_root is None:
        from src.state_locking import DEFAULT_LOCK_DIR
        lock_dir = DEFAULT_LOCK_DIR
    else:
        lock_dir = project_root / "data" / "locks"
    return cleanup_stale_locks(lock_dir, max_age_seconds, dry_run)


if __name__ == "__main__":
    # CLI tool for manual cleanup
    import argparse
    
    parser = argparse.ArgumentParser(description="Remove lock files that no process holds")
    parser.add_argument("--max-age", type=float, default=300.0, help="Minimum age in seconds before a FREE lock file is removed (default: 300); held locks are never removed")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be cleaned")
    parser.add_argument("--lock-dir", type=Path, help="Lock directory (default: auto-detect)")
    
    args = parser.parse_args()
    
    if args.lock_dir:
        lock_dir = Path(args.lock_dir)
    else:
        from src.state_locking import DEFAULT_LOCK_DIR
        lock_dir = DEFAULT_LOCK_DIR
    
    print(f"🔍 Checking lock files in: {lock_dir}")
    print(f"   Max age: {args.max_age}s ({args.max_age/60:.1f} minutes)")
    print(f"   Mode: {'DRY RUN' if args.dry_run else 'CLEANUP'}")
    print()
    
    result = cleanup_stale_locks(lock_dir, args.max_age, args.dry_run)
    
    print(f"📊 Results:")
    print(f"   Cleaned: {result['cleaned']}")
    print(f"   Kept: {result['kept']}")
    print(f"   Errors: {result['errors']}")
    print()
    
    if result['cleaned'] > 0:
        print("🗑️  Cleaned locks:")
        for item in result['cleaned_locks']:
            print(f"   - {item['lock_file']}: {item['reason']}")
        print()
    
    if result['kept'] > 0:
        print("Kept locks (held, or free but recent):")
        for item in result['kept_locks']:
            print(f"   - {item['lock_file']}: {item['reason']}")

