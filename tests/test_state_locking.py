"""
Tests for src/state_locking.py - State lock manager and process liveness checking.

Covers:
- is_process_alive: current PID, non-existent PID, edge cases
- StateLockManager.__init__: directory creation, parameter defaults
- _check_and_clean_stale_lock: non-existent file, unheld lock, corrupted JSON,
  empty file, missing PID, dead-process PID, IOError on open
- acquire_agent_lock (sync): acquire/release, lock file contents, sequential
  re-acquisition, exception release, independent agents, timeout, auto_cleanup,
  mock fcntl contention, cleanup disabled
- acquire_agent_lock_async: basic acquire/release, exception release, timeout,
  sequential re-acquisition
"""

import asyncio
import fcntl
import json
import os
import time
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.state_locking import is_process_alive, StateLockManager


# ============================================================================
# is_process_alive
# ============================================================================

class TestIsProcessAlive:

    def test_current_process_alive(self):
        """Current process PID should be alive."""
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        """Very large PID that almost certainly does not exist."""
        assert is_process_alive(999999999) is False

    def test_negative_pid(self):
        """Negative PID is platform-dependent -- just verify no crash."""
        result = is_process_alive(-1)
        assert isinstance(result, bool)

    def test_zero_pid(self):
        """PID 0 is platform-dependent -- just verify no crash."""
        result = is_process_alive(0)
        assert isinstance(result, bool)

    def test_pid_1_returns_bool(self):
        """PID 1 (launchd / init) -- result is platform/sandbox dependent."""
        result = is_process_alive(1)
        assert isinstance(result, bool)


# ============================================================================
# StateLockManager - init
# ============================================================================

class TestStateLockManagerInit:

    def test_creates_lock_dir(self, tmp_path):
        lock_dir = tmp_path / "locks"
        assert not lock_dir.exists()
        StateLockManager(lock_dir=lock_dir)
        assert lock_dir.exists()

    def test_creates_nested_lock_dir(self, tmp_path):
        """mkdir(parents=True) should create intermediate directories."""
        lock_dir = tmp_path / "a" / "b" / "c"
        StateLockManager(lock_dir=lock_dir)
        assert lock_dir.exists()

    def test_auto_cleanup_default(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path)
        assert mgr.auto_cleanup_stale is True

    def test_stale_threshold_default(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path)
        assert mgr.stale_threshold == 60.0

    def test_custom_stale_threshold(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path, stale_threshold=120.0)
        assert mgr.stale_threshold == 120.0

    def test_auto_cleanup_disabled(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)
        assert mgr.auto_cleanup_stale is False

    def test_lock_dir_stored(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path)
        assert mgr.lock_dir == tmp_path

    def test_default_lock_dir_when_none(self):
        """When lock_dir is None, it should derive from the project root."""
        mgr = StateLockManager(lock_dir=None)
        assert mgr.lock_dir.exists()
        assert str(mgr.lock_dir).endswith("data/locks")


# ============================================================================
# Lock-dir self-heal (regression for the 2026-06-19 fleet-wide check-in outage)
#
# The lock dir is gitignored and was physically removed out from under the
# long-running server (a `git worktree remove`/`add` rebuild of the deploy
# worktree). Because StateLockManager only mkdir'd the dir in __init__, every
# subsequent acquire raised ENOENT on os.open until the server restarted.
# Acquisition must now re-create the dir on demand.
# ============================================================================

class TestLockDirSelfHeal:

    def test_acquire_recreates_removed_lock_dir(self, tmp_path):
        """Sync acquire must succeed even if the lock dir was deleted post-init."""
        lock_dir = tmp_path / "locks"
        mgr = StateLockManager(lock_dir=lock_dir)
        assert lock_dir.exists()

        # Simulate the worktree-rebuild / git-clean / rm that removed the dir.
        import shutil
        shutil.rmtree(lock_dir)
        assert not lock_dir.exists()

        # Acquisition should re-create the dir instead of raising ENOENT.
        with mgr.acquire_agent_lock("healed_agent", timeout=2.0, max_retries=1):
            assert lock_dir.exists()
            assert (lock_dir / "healed_agent.lock").exists()

    @pytest.mark.asyncio
    async def test_async_acquire_recreates_removed_lock_dir(self, tmp_path, monkeypatch):
        """Async acquire must also self-heal a removed lock dir (fcntl backend)."""
        import shutil
        monkeypatch.setenv("UNITARES_AGENT_LOCK_BACKEND", "fcntl")
        lock_dir = tmp_path / "locks"
        mgr = StateLockManager(lock_dir=lock_dir)
        shutil.rmtree(lock_dir)
        assert not lock_dir.exists()

        async with mgr.acquire_agent_lock_async("healed_async", timeout=2.0, max_retries=1):
            assert lock_dir.exists()
            assert (lock_dir / "healed_async.lock").exists()

    def test_ensure_lock_dir_idempotent(self, tmp_path):
        """_ensure_lock_dir is safe to call repeatedly (exist_ok)."""
        mgr = StateLockManager(lock_dir=tmp_path / "locks")
        mgr._ensure_lock_dir()
        mgr._ensure_lock_dir()
        assert mgr.lock_dir.exists()


# ============================================================================
# _check_and_clean_stale_lock
# ============================================================================

class TestCheckAndCleanStaleLock:

    def test_nonexistent_lock_returns_false(self, tmp_path):
        mgr = StateLockManager(lock_dir=tmp_path)
        result = mgr._check_and_clean_stale_lock(tmp_path / "missing.lock")
        assert result is False

    def test_unheld_lock_cleaned(self, tmp_path):
        """Lock file that is NOT held by any process should be cleaned."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "stale.lock"
        lock_file.write_text(json.dumps({
            "pid": 999999999,
            "timestamp": time.time() - 300
        }))

        result = mgr._check_and_clean_stale_lock(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_corrupted_json_cleaned(self, tmp_path):
        """Lock file with corrupted JSON should be cleaned if not held."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "corrupt.lock"
        lock_file.write_text("not valid json {{{")

        result = mgr._check_and_clean_stale_lock(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_empty_lock_cleaned(self, tmp_path):
        """Empty lock file should be cleaned if not held."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "empty.lock"
        lock_file.write_text("")

        result = mgr._check_and_clean_stale_lock(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_lock_with_no_pid_cleaned(self, tmp_path):
        """Lock without PID field should be cleaned if not held."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "no_pid.lock"
        lock_file.write_text(json.dumps({"timestamp": time.time()}))

        result = mgr._check_and_clean_stale_lock(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_lock_with_dead_pid_cleaned(self, tmp_path):
        """Lock referencing a dead PID should be cleaned if not held."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "dead.lock"
        lock_file.write_text(json.dumps({
            "pid": 999999999,
            "timestamp": time.time()  # recent timestamp
        }))

        result = mgr._check_and_clean_stale_lock(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_actively_held_lock_not_cleaned(self, tmp_path):
        """Lock that is actively held (flock) should NOT be cleaned."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "held.lock"

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_info = json.dumps({
            "pid": os.getpid(),
            "timestamp": time.time()
        })
        os.write(fd, lock_info.encode())

        try:
            result = mgr._check_and_clean_stale_lock(lock_file)
            assert result is False
            assert lock_file.exists()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_ioerror_on_open_returns_false(self, tmp_path):
        """If os.open fails for the lock file, should return False (safe)."""
        mgr = StateLockManager(lock_dir=tmp_path)
        lock_file = tmp_path / "ioerr.lock"
        lock_file.write_text("data")

        with patch("os.open", side_effect=IOError("mocked open error")):
            result = mgr._check_and_clean_stale_lock(lock_file)
            assert result is False


# ============================================================================
# acquire_agent_lock - sync
# ============================================================================

class TestAcquireAgentLock:

    def test_acquire_and_release(self, tmp_path):
        """Should acquire lock and release on context exit."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with mgr.acquire_agent_lock("test_agent", timeout=2.0, max_retries=1):
            lock_file = tmp_path / "test_agent.lock"
            assert lock_file.exists()

    def test_lock_file_contains_pid_and_timestamp(self, tmp_path):
        """Lock file should contain JSON with pid, timestamp, agent_id."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with mgr.acquire_agent_lock("info_agent", timeout=2.0, max_retries=1):
            lock_file = tmp_path / "info_agent.lock"
            with open(lock_file, "r") as f:
                data = json.loads(f.read())
            assert data["pid"] == os.getpid()
            assert data["agent_id"] == "info_agent"
            assert "timestamp" in data
            assert isinstance(data["timestamp"], float)
            # Timestamp should be recent (within 10 seconds)
            assert abs(data["timestamp"] - time.time()) < 10

    def test_sequential_acquisitions_succeed(self, tmp_path):
        """Two sequential acquisitions of the same agent should both succeed."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with mgr.acquire_agent_lock("seq_agent", timeout=2.0, max_retries=1):
            pass

        with mgr.acquire_agent_lock("seq_agent", timeout=2.0, max_retries=1):
            pass

    def test_lock_released_on_exception(self, tmp_path):
        """Lock should be released even if an exception occurs inside."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with pytest.raises(ValueError):
            with mgr.acquire_agent_lock("exc_test", timeout=2.0, max_retries=1):
                raise ValueError("test error")

        # Should be able to acquire again
        with mgr.acquire_agent_lock("exc_test", timeout=2.0, max_retries=1):
            pass

    def test_different_agents_independent(self, tmp_path):
        """Different agent IDs should have independent locks."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with mgr.acquire_agent_lock("agent_A", timeout=2.0, max_retries=1):
            with mgr.acquire_agent_lock("agent_B", timeout=2.0, max_retries=1):
                assert (tmp_path / "agent_A.lock").exists()
                assert (tmp_path / "agent_B.lock").exists()

    def test_timeout_raises_error(self, tmp_path):
        """Should raise TimeoutError when lock cannot be acquired."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)
        lock_file = tmp_path / "held_agent.lock"

        # Hold a lock using low-level fcntl
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with pytest.raises(TimeoutError):
                with mgr.acquire_agent_lock("held_agent", timeout=0.3, max_retries=1):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_timeout_error_message_contains_agent_id(self, tmp_path):
        """TimeoutError message should mention the agent_id."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)
        lock_file = tmp_path / "msg_agent.lock"

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with pytest.raises(TimeoutError, match="msg_agent"):
                with mgr.acquire_agent_lock("msg_agent", timeout=0.2, max_retries=1):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_auto_cleanup_stale_before_acquire(self, tmp_path):
        """Stale lock should be cleaned before attempting acquisition."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=True)
        lock_file = tmp_path / "stale_agent.lock"

        # Create a stale lock file (no process holding it)
        lock_file.write_text(json.dumps({
            "pid": 999999999,
            "timestamp": time.time() - 300
        }))

        # Should succeed because stale lock gets cleaned
        with mgr.acquire_agent_lock("stale_agent", timeout=2.0, max_retries=2):
            pass

    def test_auto_cleanup_disabled_skips_cleanup(self, tmp_path):
        """When auto_cleanup_stale=False, _check_and_clean_stale_lock is not called."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)

        with patch.object(mgr, "_check_and_clean_stale_lock") as mock_clean:
            with mgr.acquire_agent_lock("no_clean", timeout=2.0, max_retries=1):
                pass
            # Should never have been called (no stale lock to trigger retry cleanup either)
            mock_clean.assert_not_called()

    def test_mock_flock_ioerror_causes_timeout(self, tmp_path):
        """When fcntl.flock always raises IOError, acquisition should time out."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)

        with patch("fcntl.flock", side_effect=IOError("mocked contention")):
            with pytest.raises(IOError):
                with mgr.acquire_agent_lock("contended", timeout=0.2, max_retries=1):
                    pass

    def test_lock_file_path_matches_agent_id(self, tmp_path):
        """Lock file name should be {agent_id}.lock."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with mgr.acquire_agent_lock("my-agent-123", timeout=2.0, max_retries=1):
            assert (tmp_path / "my-agent-123.lock").exists()

    def test_multiple_retries_with_contention(self, tmp_path):
        """Verify that multiple retries are attempted before giving up."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)
        lock_file = tmp_path / "retry_agent.lock"

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            start = time.time()
            with pytest.raises(TimeoutError):
                with mgr.acquire_agent_lock("retry_agent", timeout=0.15, max_retries=2):
                    pass
            elapsed = time.time() - start
            # With 2 retries at 0.15s timeout each plus backoff, should take > 0.3s
            assert elapsed >= 0.3
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_stale_cleanup_exception_does_not_block_acquisition(self, tmp_path):
        """If _check_and_clean_stale_lock raises, acquisition should still proceed."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=True)

        with patch.object(
            mgr, "_check_and_clean_stale_lock", side_effect=RuntimeError("cleanup boom")
        ):
            # Should still succeed -- cleanup error is caught
            with mgr.acquire_agent_lock("clean_err", timeout=2.0, max_retries=1):
                pass

    def test_lock_contention_then_release_succeeds(self, tmp_path):
        """If a lock is released while we are retrying, we should acquire it."""
        import threading

        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=False)
        lock_file = tmp_path / "race_agent.lock"

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release_after_delay():
            time.sleep(0.3)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        t = threading.Thread(target=release_after_delay)
        t.start()

        try:
            # Should eventually acquire (timeout=2s gives plenty of room)
            with mgr.acquire_agent_lock("race_agent", timeout=2.0, max_retries=2):
                pass
        finally:
            t.join(timeout=5)


# ============================================================================
# acquire_agent_lock_async
# ============================================================================

class TestAcquireAgentLockAsync:
    """File-lock (fcntl) backend behavior of the async lock.

    These assert on-disk ``.lock`` semantics, so they pin the fcntl backend;
    the advisory backend (now the default) is covered separately below.
    """

    @pytest.fixture(autouse=True)
    def _pin_fcntl_backend(self, monkeypatch):
        monkeypatch.setenv("UNITARES_AGENT_LOCK_BACKEND", "fcntl")

    @pytest.mark.asyncio
    async def test_async_acquire_and_release(self, tmp_path):
        """Async lock should acquire and release correctly."""
        mgr = StateLockManager(lock_dir=tmp_path)

        async with mgr.acquire_agent_lock_async("async_test", timeout=2.0, max_retries=1):
            lock_file = tmp_path / "async_test.lock"
            assert lock_file.exists()

    @pytest.mark.asyncio
    async def test_async_lock_file_contents(self, tmp_path):
        """Async lock file should contain correct JSON metadata."""
        mgr = StateLockManager(lock_dir=tmp_path)

        async with mgr.acquire_agent_lock_async("async_info", timeout=2.0, max_retries=1):
            lock_file = tmp_path / "async_info.lock"
            with open(lock_file, "r") as f:
                data = json.loads(f.read())
            assert data["pid"] == os.getpid()
            assert data["agent_id"] == "async_info"
            assert isinstance(data["timestamp"], float)

    @pytest.mark.asyncio
    async def test_async_sequential_acquisitions(self, tmp_path):
        """Two sequential async acquisitions of same agent should succeed."""
        mgr = StateLockManager(lock_dir=tmp_path)

        async with mgr.acquire_agent_lock_async("async_seq", timeout=2.0, max_retries=1):
            pass

        async with mgr.acquire_agent_lock_async("async_seq", timeout=2.0, max_retries=1):
            pass

    @pytest.mark.asyncio
    async def test_async_lock_released_on_exception(self, tmp_path):
        """Async lock should release on exception."""
        mgr = StateLockManager(lock_dir=tmp_path)

        with pytest.raises(ValueError):
            async with mgr.acquire_agent_lock_async("async_exc", timeout=2.0, max_retries=1):
                raise ValueError("async test error")

        # Should be able to acquire again
        async with mgr.acquire_agent_lock_async("async_exc", timeout=2.0, max_retries=1):
            pass

    @pytest.mark.asyncio
    async def test_async_timeout_raises_error(self, tmp_path):
        """Async lock should raise TimeoutError on timeout.

        Note: auto_cleanup_stale must be True here because the async method
        contains local `import asyncio` statements inside the cleanup branch.
        When cleanup is disabled, Python's scoping still treats `asyncio` as
        local (due to those import statements) but never binds it, causing
        UnboundLocalError on `await asyncio.sleep(...)`.
        """
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=True)
        lock_file = tmp_path / "async_held.lock"

        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with pytest.raises(TimeoutError):
                async with mgr.acquire_agent_lock_async("async_held", timeout=0.3, max_retries=1):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @pytest.mark.asyncio
    async def test_async_different_agents_independent(self, tmp_path):
        """Different agent IDs should have independent async locks."""
        mgr = StateLockManager(lock_dir=tmp_path)

        async with mgr.acquire_agent_lock_async("async_A", timeout=2.0, max_retries=1):
            async with mgr.acquire_agent_lock_async("async_B", timeout=2.0, max_retries=1):
                assert (tmp_path / "async_A.lock").exists()
                assert (tmp_path / "async_B.lock").exists()

    @pytest.mark.asyncio
    async def test_async_auto_cleanup_before_acquire(self, tmp_path):
        """Async acquisition should clean stale locks before acquiring."""
        mgr = StateLockManager(lock_dir=tmp_path, auto_cleanup_stale=True)
        lock_file = tmp_path / "async_stale.lock"

        lock_file.write_text(json.dumps({
            "pid": 999999999,
            "timestamp": time.time() - 300
        }))

        async with mgr.acquire_agent_lock_async("async_stale", timeout=2.0, max_retries=2):
            pass

    @pytest.mark.asyncio
    async def test_async_does_not_block_event_loop(self, tmp_path):
        """Async lock should use asyncio.sleep, allowing other tasks to run."""
        mgr = StateLockManager(lock_dir=tmp_path)
        other_task_ran = False

        async def other_task():
            nonlocal other_task_ran
            await asyncio.sleep(0)
            other_task_ran = True

        async with mgr.acquire_agent_lock_async("async_nonblock", timeout=2.0, max_retries=1):
            await other_task()

        assert other_task_ran


# ============================================================================
# acquire_agent_lock_async — advisory (PostgreSQL) backend (default)
# ============================================================================

class _FakeConn:
    """Records pg_try_advisory_lock / pg_advisory_unlock calls; scriptable acquire result."""

    def __init__(self, acquire_results):
        # acquire_results: list of truthy/falsy values returned by successive
        # pg_try_advisory_lock calls (last value repeats once exhausted).
        self._acquire_results = list(acquire_results)
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            if len(self._acquire_results) > 1:
                return self._acquire_results.pop(0)
            return self._acquire_results[0] if self._acquire_results else True
        if "pg_advisory_unlock" in sql:
            return True
        return None


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class TestAcquireAgentLockAsyncAdvisory:
    """Advisory (PostgreSQL) backend — the default. get_db() is mocked so these
    run without a live database."""

    @pytest.mark.asyncio
    async def test_advisory_is_default_backend(self, tmp_path, monkeypatch):
        """With no env override, the advisory path runs (no .lock file written)."""
        monkeypatch.delenv("UNITARES_AGENT_LOCK_BACKEND", raising=False)
        conn = _FakeConn([True])
        monkeypatch.setattr("src.db.get_db", lambda: _FakeDB(conn))

        mgr = StateLockManager(lock_dir=tmp_path)
        async with mgr.acquire_agent_lock_async("adv_agent", timeout=2.0, max_retries=1):
            # No file lock is created on the advisory path.
            assert not (tmp_path / "adv_agent.lock").exists()

        sqls = " ".join(c[0] for c in conn.calls)
        assert "pg_try_advisory_lock" in sqls
        assert "pg_advisory_unlock" in sqls

    @pytest.mark.asyncio
    async def test_advisory_acquire_and_unlock_same_key(self, tmp_path, monkeypatch):
        """Lock and unlock are issued for the same agent id (hashtext key)."""
        monkeypatch.setenv("UNITARES_AGENT_LOCK_BACKEND", "advisory")
        conn = _FakeConn([True])
        monkeypatch.setattr("src.db.get_db", lambda: _FakeDB(conn))

        mgr = StateLockManager(lock_dir=tmp_path)
        async with mgr.acquire_agent_lock_async("same_key", timeout=2.0, max_retries=1):
            pass

        lock_calls = [c for c in conn.calls if "pg_try_advisory_lock" in c[0]]
        unlock_calls = [c for c in conn.calls if "pg_advisory_unlock" in c[0]]
        assert lock_calls and unlock_calls
        assert lock_calls[0][1] == ("same_key",)
        assert unlock_calls[0][1] == ("same_key",)

    @pytest.mark.asyncio
    async def test_advisory_unlock_runs_on_exception(self, tmp_path, monkeypatch):
        """The advisory lock is released even when the body raises."""
        monkeypatch.setenv("UNITARES_AGENT_LOCK_BACKEND", "advisory")
        conn = _FakeConn([True])
        monkeypatch.setattr("src.db.get_db", lambda: _FakeDB(conn))

        mgr = StateLockManager(lock_dir=tmp_path)
        with pytest.raises(ValueError):
            async with mgr.acquire_agent_lock_async("adv_exc", timeout=2.0, max_retries=1):
                raise ValueError("boom")

        assert any("pg_advisory_unlock" in c[0] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_advisory_timeout_raises_timeouterror(self, tmp_path, monkeypatch):
        """When pg_try_advisory_lock never succeeds, TimeoutError is raised and
        no unlock is issued (we never held the lock)."""
        monkeypatch.setenv("UNITARES_AGENT_LOCK_BACKEND", "advisory")
        conn = _FakeConn([False])  # never acquires
        monkeypatch.setattr("src.db.get_db", lambda: _FakeDB(conn))

        mgr = StateLockManager(lock_dir=tmp_path)
        with pytest.raises(TimeoutError):
            async with mgr.acquire_agent_lock_async("adv_to", timeout=0.1, max_retries=1):
                pass

        # The poll loop must actually have run (more than one try) before giving up...
        assert len([c for c in conn.calls if "pg_try_advisory_lock" in c[0]]) > 1
        # ...and no unlock is issued, because the lock was never held.
        assert not any("pg_advisory_unlock" in c[0] for c in conn.calls)
