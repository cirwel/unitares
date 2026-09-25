"""
Tests for src/lock_cleanup.py - Stale lock file detection and cleanup.

Tests is_process_alive, check_lock_staleness, cleanup_stale_locks,
cleanup_stale_state_locks using tmp_path fixtures for file I/O isolation.
"""

import fcntl
import json
import os
import time
import pytest
import sys
from contextlib import contextmanager
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
# helpers
# ============================================================================

def _write_lock(path, pid=999999999, age_seconds=600.0, content=None):
    """A lock file nobody holds, last touched ``age_seconds`` ago."""
    path.write_text(content if content is not None else json.dumps(
        {"pid": pid, "timestamp": time.time() - age_seconds}
    ))
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


@contextmanager
def _held(path):
    """Hold an exclusive flock on ``path``, as a live lock holder would."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ============================================================================
# check_lock_staleness
# ============================================================================

class TestCheckLockStaleness:
    """Stale means: no process holds the flock, and the file is old enough.
    The recorded pid and timestamp never make a held lock stale."""

    def test_nonexistent_file(self, tmp_path):
        is_stale, reason = check_lock_staleness(tmp_path / "missing.lock")
        assert is_stale is False
        assert "doesn't exist" in reason

    def test_free_and_old_is_stale(self, tmp_path):
        lock = _write_lock(tmp_path / "old.lock", pid=os.getpid())
        is_stale, reason = check_lock_staleness(lock, max_age_seconds=300)
        assert is_stale is True
        assert "not held" in reason

    def test_free_but_recent_is_not_stale(self, tmp_path):
        lock = _write_lock(tmp_path / "recent.lock", age_seconds=5)
        is_stale, reason = check_lock_staleness(lock, max_age_seconds=300)
        assert is_stale is False
        assert "free, but touched" in reason

    def test_held_lock_is_never_stale(self, tmp_path):
        """Dead recorded pid, ancient mtime, max_age 0: still held, so kept.
        The old age/pid rule deleted exactly this and admitted a second writer."""
        lock = _write_lock(tmp_path / "held.lock", pid=999999999, age_seconds=10_000)
        with _held(lock):
            old = time.time() - 10_000
            os.utime(lock, (old, old))
            is_stale, reason = check_lock_staleness(lock, max_age_seconds=0)
        assert is_stale is False
        assert "held by a live process" in reason
        assert "999999999" in reason

    @pytest.mark.parametrize("content", ["not valid json {{{", "", json.dumps({"timestamp": 1})])
    def test_content_does_not_decide(self, tmp_path, content):
        """Corrupt, empty or pid-less files are judged by held-ness alone."""
        lock = _write_lock(tmp_path / "odd.lock", content=content)
        assert check_lock_staleness(lock, max_age_seconds=300)[0] is True
        with _held(lock):
            assert check_lock_staleness(lock, max_age_seconds=0)[0] is False

    @pytest.mark.parametrize(("content", "expected"), [
        (json.dumps({"pid": os.getpid()}), f"recorded pid {os.getpid()}, alive"),
        (str(os.getpid()), f"recorded pid {os.getpid()}, alive"),  # DistributedLock format
        ("", "holder pid unknown"),  # truncated before the holder rewrote it
        ("not json", "holder pid unknown"),
    ])
    def test_held_reason_reports_the_pid_it_could_read(self, tmp_path, content, expected):
        """An unreadable pid is 'unknown', never 'not running': the tool must
        not suggest a holder is dead when it could not tell."""
        lock = _write_lock(tmp_path / "held.lock", content=content)
        with _held(lock):
            is_stale, reason = check_lock_staleness(lock, max_age_seconds=0)
        assert is_stale is False
        assert expected in reason
        assert "None" not in reason

    def test_probe_does_not_remove(self, tmp_path):
        lock = _write_lock(tmp_path / "old.lock")
        check_lock_staleness(lock, max_age_seconds=0)
        assert lock.exists()


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
        result = cleanup_stale_locks(tmp_path / "missing_dir")
        assert result["cleaned"] == 0

    def test_removes_free_lock(self, tmp_path):
        lock = _write_lock(tmp_path / "free.lock")
        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 1
        assert not lock.exists()

    def test_keeps_free_lock_younger_than_max_age(self, tmp_path):
        lock = _write_lock(tmp_path / "recent.lock", age_seconds=5)
        result = cleanup_stale_locks(tmp_path, max_age_seconds=300)
        assert result["kept"] == 1
        assert lock.exists()

    def test_never_removes_a_held_lock(self, tmp_path):
        """max_age 0 and a dead recorded pid: the lock is held, so it stays."""
        lock = _write_lock(tmp_path / "held.lock", pid=999999999)
        with _held(lock):
            result = cleanup_stale_locks(tmp_path, max_age_seconds=0)
            assert lock.exists()
        assert result["cleaned"] == 0
        assert result["kept"] == 1
        assert "held" in result["kept_locks"][0]["reason"]
        assert "recorded pid 999999999" in result["kept_locks"][0]["reason"]

    def test_never_removes_a_lock_held_by_state_lock_manager(self, tmp_path):
        """The review repro: sweeping inside acquire_agent_lock with max_age 0
        used to delete a1.lock and let a second process lock a fresh file."""
        from src.state_locking import StateLockManager

        mgr = StateLockManager(lock_dir=tmp_path)
        with mgr.acquire_agent_lock("a1"):
            result = cleanup_stale_locks(tmp_path, max_age_seconds=0)
            assert (tmp_path / "a1.lock").exists()
            fd = os.open(str(tmp_path / "a1.lock"), os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        assert result["cleaned"] == 0

    def test_dry_run(self, tmp_path):
        """Dry run should report but not delete."""
        lock = _write_lock(tmp_path / "free.lock")
        result = cleanup_stale_locks(tmp_path, dry_run=True)
        assert result["cleaned"] == 1
        assert result["dry_run"] is True
        assert lock.exists()

    def test_mixed_locks(self, tmp_path):
        held = _write_lock(tmp_path / "held.lock", pid=os.getpid())
        _write_lock(tmp_path / "free.lock")
        _write_lock(tmp_path / "corrupt.lock", content="not json")
        with _held(held):
            result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 2  # free + corrupt
        assert result["kept"] == 1  # held

    def test_removal_errors_are_counted_and_listed(self, tmp_path):
        _write_lock(tmp_path / "stuck.lock")
        with patch("src.lock_cleanup.remove_lock_file_if_free", side_effect=PermissionError("read-only dir")):
            result = cleanup_stale_locks(tmp_path)
        assert result["errors"] == 1
        assert result["error_locks"] == [{"lock_file": "stuck.lock", "error": "read-only dir"}]

    def test_removes_a_free_read_only_lock_file(self, tmp_path):
        """flock needs no write access, and unlink permission comes from the
        directory, so a free 0444 file is removable like any other."""
        lock = _write_lock(tmp_path / "ro.lock")
        os.chmod(lock, 0o444)
        result = cleanup_stale_locks(tmp_path)
        assert result["cleaned"] == 1
        assert not lock.exists()

    def test_an_unreadable_lock_file_is_an_error_not_kept(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root can open a mode-000 file")
        lock = _write_lock(tmp_path / "locked-out.lock")
        os.chmod(lock, 0o000)
        try:
            result = cleanup_stale_locks(tmp_path)
        finally:
            os.chmod(lock, 0o644)
        assert result["kept"] == 0
        assert result["errors"] == 1
        assert result["error_locks"][0]["lock_file"] == "locked-out.lock"
        assert "cannot open" in result["error_locks"][0]["error"]

    def test_a_file_gone_before_the_probe_is_not_counted(self, tmp_path):
        _write_lock(tmp_path / "gone.lock")
        with patch("src.lock_cleanup.remove_lock_file_if_free",
                   return_value=(False, "lock file doesn't exist")):
            result = cleanup_stale_locks(tmp_path)
        assert (result["cleaned"], result["kept"], result["errors"]) == (0, 0, 0)

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
        _write_lock(tmp_path / "bad.lock", content="corrupt data")

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

        lock = _write_lock(lock_dir / "test.lock")

        result = cleanup_stale_state_locks(project_root=tmp_path)
        assert result["cleaned"] == 1

    def test_default_sweeps_the_state_lock_managers_dir(self, tmp_path, monkeypatch):
        """Without project_root, sweep the directory StateLockManager writes
        to, so a UNITARES_LOCK_DIR override moves writer and sweeper together."""
        import src.state_locking as state_locking

        lock_dir = tmp_path / "elsewhere"
        lock_dir.mkdir()
        monkeypatch.setattr(state_locking, "DEFAULT_LOCK_DIR", lock_dir)
        lock = _write_lock(lock_dir / "test.lock")

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
