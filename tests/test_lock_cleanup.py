"""
Tests for src/lock_cleanup.py - Stale lock file detection and cleanup.

Tests is_process_alive, check_lock_staleness, cleanup_stale_locks,
cleanup_stale_state_locks using tmp_path fixtures for file I/O isolation.
"""

import json
import os
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.lock_cleanup import (
    is_process_alive,
    check_lock_staleness,
    cleanup_stale_locks,
    cleanup_stale_state_locks,
)


# ============================================================================
# is_process_alive
# ============================================================================

class TestIsProcessAlive:

    def test_current_process_alive(self):
        """Current process PID should be alive."""
        assert is_process_alive(os.getpid()) is True

    def test_pid_1_alive(self):
        """PID 1 (init/launchd) should be alive on any system."""
        assert is_process_alive(1) is True

    def test_nonexistent_pid(self):
        """Very large PID should not exist."""
        assert is_process_alive(999999999) is False

    def test_negative_pid(self):
        """Negative PID should return False."""
        assert is_process_alive(-1) is False

    def test_zero_pid(self):
        """PID 0 - depends on platform, shouldn't crash."""
        result = is_process_alive(0)
        assert isinstance(result, bool)


# ============================================================================
# check_lock_staleness
# ============================================================================

class TestCheckLockStaleness:

    def test_nonexistent_file(self, tmp_path):
        """Non-existent lock file should return not stale."""
        lock = tmp_path / "missing.lock"
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is False
        assert "doesn't exist" in reason

    def test_valid_lock_current_process(self, tmp_path):
        """Lock with current PID and recent timestamp should not be stale."""
        lock = tmp_path / "active.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time()
        }))
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is False
        assert "active" in reason

    def test_stale_by_age(self, tmp_path):
        """Old lock file should be stale."""
        lock = tmp_path / "old.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time()
        }))
        # Set mtime to old
        old_time = time.time() - 600
        os.utime(lock, (old_time, old_time))
        is_stale, reason = check_lock_staleness(lock, max_age_seconds=300)
        assert is_stale is True
        assert "age" in reason.lower()

    def test_stale_by_dead_process(self, tmp_path):
        """Lock with dead PID should be stale."""
        lock = tmp_path / "dead.lock"
        lock.write_text(json.dumps({
            "pid": 999999999,  # Non-existent PID
            "timestamp": time.time()
        }))
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is True
        assert "not running" in reason

    def test_stale_no_pid(self, tmp_path):
        """Lock without PID should be stale."""
        lock = tmp_path / "no_pid.lock"
        lock.write_text(json.dumps({"timestamp": time.time()}))
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is True
        assert "no PID" in reason

    def test_stale_old_timestamp(self, tmp_path):
        """Lock with old timestamp in data (but recent mtime) should be stale."""
        lock = tmp_path / "old_ts.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time() - 600  # Old timestamp in data
        }))
        is_stale, reason = check_lock_staleness(lock, max_age_seconds=300)
        assert is_stale is True
        assert "timestamp age" in reason.lower()

    def test_corrupted_json(self, tmp_path):
        """Corrupted JSON should be stale."""
        lock = tmp_path / "corrupt.lock"
        lock.write_text("not valid json {{{")
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is True
        assert "unreadable" in reason

    def test_empty_file(self, tmp_path):
        """Empty lock file should be stale."""
        lock = tmp_path / "empty.lock"
        lock.write_text("")
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is True
        assert "unreadable" in reason

    def test_custom_max_age(self, tmp_path):
        """Custom max_age should be respected."""
        lock = tmp_path / "short.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time() - 10
        }))
        # With max_age=5, this should be stale
        is_stale, reason = check_lock_staleness(lock, max_age_seconds=5)
        assert is_stale is True

    def test_zero_timestamp_skips_timestamp_check(self, tmp_path):
        """timestamp=0 should skip timestamp age check."""
        lock = tmp_path / "zero_ts.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": 0
        }))
        is_stale, reason = check_lock_staleness(lock)
        assert is_stale is False
        assert "active" in reason


# ============================================================================
# cleanup_stale_locks
# ============================================================================

class TestCleanupStaleLocks:

    def test_empty_dir(self, tmp_path):
        """Empty directory should report 0 cleaned."""
        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 0
        assert result["kept"] == 0

    def test_nonexistent_dir(self, tmp_path):
        """Non-existent directory should not crash."""
        missing = tmp_path / "missing_dir"
        result = cleanup_stale_locks(missing)
        assert result["cleaned"] == 0

    def test_cleans_stale_locks(self, tmp_path):
        """Stale lock files should be cleaned."""
        # Create a stale lock (dead PID)
        lock = tmp_path / "stale.lock"
        lock.write_text(json.dumps({"pid": 999999999, "timestamp": time.time()}))

        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 1
        assert not lock.exists()

    def test_keeps_active_locks(self, tmp_path):
        """Active lock files should be kept."""
        lock = tmp_path / "active.lock"
        lock.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time()
        }))

        result = cleanup_stale_locks(tmp_path)
        assert result["kept"] == 1
        assert lock.exists()

    def test_dry_run(self, tmp_path):
        """Dry run should report but not delete."""
        lock = tmp_path / "stale.lock"
        lock.write_text(json.dumps({"pid": 999999999, "timestamp": time.time()}))

        result = cleanup_stale_locks(tmp_path, dry_run=True)
        assert result["cleaned"] == 1
        assert result["dry_run"] is True
        assert lock.exists()  # Not deleted

    def test_mixed_locks(self, tmp_path):
        """Mix of stale and active locks."""
        # Active lock
        active = tmp_path / "active.lock"
        active.write_text(json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time()
        }))
        # Stale lock (dead PID)
        stale = tmp_path / "stale.lock"
        stale.write_text(json.dumps({"pid": 999999999, "timestamp": time.time()}))
        # Corrupt lock
        corrupt = tmp_path / "corrupt.lock"
        corrupt.write_text("not json")

        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 2  # stale + corrupt
        assert result["kept"] == 1  # active

    def test_only_processes_lock_files(self, tmp_path):
        """Non-.lock files should be ignored."""
        (tmp_path / "not_a_lock.txt").write_text("data")
        (tmp_path / "also_not.json").write_text("{}")

        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 0
        assert result["kept"] == 0
        assert (tmp_path / "not_a_lock.txt").exists()

    def test_cleaned_locks_have_details(self, tmp_path):
        """Cleaned locks should include lock_file and reason."""
        lock = tmp_path / "bad.lock"
        lock.write_text("corrupt data")

        result = cleanup_stale_locks(tmp_path)
        assert len(result["cleaned_locks"]) == 1
        assert "lock_file" in result["cleaned_locks"][0]
        assert "reason" in result["cleaned_locks"][0]


# ============================================================================
# cleanup_stale_state_locks
# ============================================================================

class TestCleanupStaleStateLocks:

    def test_with_explicit_project_root(self, tmp_path):
        """Should look in project_root/data/locks."""
        lock_dir = tmp_path / "data" / "locks"
        lock_dir.mkdir(parents=True)

        # Create a stale lock
        lock = lock_dir / "test.lock"
        lock.write_text(json.dumps({"pid": 999999999, "timestamp": time.time()}))

        result = cleanup_stale_state_locks(project_root=tmp_path)
        assert result["cleaned"] == 1

    def test_default_sweeps_the_state_lock_managers_dir(self, tmp_path, monkeypatch):
        """Without project_root, sweep the directory StateLockManager writes
        to, so a UNITARES_LOCK_DIR override moves writer and sweeper together."""
        import src.state_locking as state_locking

        lock_dir = tmp_path / "elsewhere"
        lock_dir.mkdir()
        monkeypatch.setattr(state_locking, "DEFAULT_LOCK_DIR", lock_dir)
        lock = lock_dir / "test.lock"
        lock.write_text(json.dumps({"pid": 999999999, "timestamp": time.time()}))

        result = cleanup_stale_state_locks()
        assert result["cleaned"] == 1
        assert not lock.exists()

    def test_missing_lock_dir(self, tmp_path):
        """Should handle missing data/locks dir gracefully."""
        result = cleanup_stale_state_locks(project_root=tmp_path)
        assert result["cleaned"] == 0

    def test_dry_run_passthrough(self, tmp_path):
        """dry_run should be passed through."""
        lock_dir = tmp_path / "data" / "locks"
        lock_dir.mkdir(parents=True)
        lock = lock_dir / "test.lock"
        lock.write_text("corrupt")

        result = cleanup_stale_state_locks(project_root=tmp_path, dry_run=True)
        assert result["dry_run"] is True
        assert lock.exists()
