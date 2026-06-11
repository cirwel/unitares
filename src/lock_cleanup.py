"""
Stale Lock Cleanup Utility

Automatically detects and removes lock files that are no longer held by active processes.
Prevents lock files from accumulating when processes crash or are killed.
"""

import os
import json
import time
from pathlib import Path
from typing import Dict, Tuple

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


def check_lock_staleness(lock_file: Path, max_age_seconds: float = 300.0) -> Tuple[bool, str]:
    """
    Check if a lock file is stale (not held by active process or too old).
    
    Args:
        lock_file: Path to lock file
        max_age_seconds: Maximum age in seconds before considering stale (default: 5 minutes)
    
    Returns:
        (is_stale, reason) tuple
    """
    if not lock_file.exists():
        return False, "lock file doesn't exist"
    
    # Check file modification time
    file_age = time.time() - lock_file.stat().st_mtime
    if file_age > max_age_seconds:
        return True, f"lock file age ({file_age:.0f}s) exceeds max_age ({max_age_seconds}s)"
    
    # Try to read lock info
    try:
        with open(lock_file, 'r') as f:
            lock_data = json.load(f)
            pid = lock_data.get('pid')
            timestamp = lock_data.get('timestamp', 0)
            
            if pid is None:
                return True, "no PID in lock file"
            
            # Check if process is alive
            if not is_process_alive(pid):
                return True, f"process {pid} is not running"
            
            # Check lock timestamp age
            if timestamp > 0:
                lock_age = time.time() - timestamp
                if lock_age > max_age_seconds:
                    return True, f"lock timestamp age ({lock_age:.0f}s) exceeds max_age ({max_age_seconds}s)"
    
    except (json.JSONDecodeError, IOError, ValueError) as e:
        # Corrupted or unreadable lock file - consider stale
        return True, f"lock file unreadable: {e}"
    
    return False, "lock is active"


def cleanup_stale_locks(lock_dir: Path, max_age_seconds: float = 300.0, dry_run: bool = False) -> Dict[str, any]:
    """
    Clean up stale lock files.
    
    Args:
        lock_dir: Directory containing lock files
        max_age_seconds: Maximum age before considering stale (default: 5 minutes)
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
            is_stale, reason = check_lock_staleness(lock_file, max_age_seconds)
            
            if is_stale:
                if not dry_run:
                    lock_file.unlink(missing_ok=True)
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
        "errors": errors,
        "dry_run": dry_run
    }


def cleanup_stale_state_locks(project_root: Path = None, max_age_seconds: float = 300.0, dry_run: bool = False) -> Dict[str, any]:
    """
    Clean up stale state lock files (convenience wrapper).
    
    Args:
        project_root: Project root directory (defaults to detecting from file location)
        max_age_seconds: Maximum age before considering stale
        dry_run: If True, only report what would be cleaned
    
    Returns:
        Dict with cleanup statistics
    """
    if project_root is None:
        # Detect project root from this file's location
        project_root = Path(__file__).parent.parent
    
    lock_dir = project_root / "data" / "locks"
    return cleanup_stale_locks(lock_dir, max_age_seconds, dry_run)


if __name__ == "__main__":
    # CLI tool for manual cleanup
    import argparse
    
    parser = argparse.ArgumentParser(description="Clean up stale lock files")
    parser.add_argument("--max-age", type=float, default=300.0, help="Maximum age in seconds (default: 300 = 5 minutes)")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be cleaned")
    parser.add_argument("--lock-dir", type=Path, help="Lock directory (default: auto-detect)")
    
    args = parser.parse_args()
    
    if args.lock_dir:
        lock_dir = Path(args.lock_dir)
    else:
        project_root = Path(__file__).parent.parent
        lock_dir = project_root / "data" / "locks"
    
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
        print("✅ Active locks:")
        for item in result['kept_locks']:
            print(f"   - {item['lock_file']}: {item['reason']}")

