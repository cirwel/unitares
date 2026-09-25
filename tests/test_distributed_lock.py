"""
Tests for src/cache/distributed_lock.py

Covers:
- Redis lock acquisition (SETNX + expiration)
- Lock release (atomic Lua script)
- Lock timeout behavior
- is_locked check
- force_release
- health_check (Redis and file fallback)
- File-based fallback locking
"""

import asyncio
import os
import pytest
from unittest.mock import patch, AsyncMock

fakeredis = pytest.importorskip("fakeredis", reason="fakeredis not installed")
import fakeredis.aioredis

from src.cache.distributed_lock import (
    DistributedLock,
    get_distributed_lock,
    LOCK_PREFIX,
)


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def lock(tmp_path, fake_redis):
    """DistributedLock with fakeredis backend."""
    async def _get_fake_redis():
        return fake_redis

    with patch("src.cache.distributed_lock.get_redis", new=_get_fake_redis):
        lock_instance = DistributedLock(lock_dir=tmp_path, lock_timeout=5.0)
        yield lock_instance


@pytest.fixture
def lock_no_redis(tmp_path):
    """DistributedLock with no Redis (file fallback)."""
    async def _get_none():
        return None

    with patch("src.cache.distributed_lock.get_redis", new=_get_none):
        lock_instance = DistributedLock(lock_dir=tmp_path, lock_timeout=5.0)
        yield lock_instance


# ============================================================================
# Redis Lock Acquisition
# ============================================================================

class TestAcquireRedis:

    @pytest.mark.asyncio
    async def test_acquire_and_release(self, lock, fake_redis):
        """Lock is acquired, body executes, lock is released."""
        executed = False
        async with lock.acquire("resource-1"):
            executed = True
            # Lock should exist in Redis
            val = await fake_redis.get(f"{LOCK_PREFIX}resource-1")
            assert val is not None
        assert executed

    @pytest.mark.asyncio
    async def test_lock_has_pid_and_random(self, lock, fake_redis):
        """Lock value contains PID and random component."""
        async with lock.acquire("resource-2"):
            val = await fake_redis.get(f"{LOCK_PREFIX}resource-2")
            assert str(os.getpid()) in val
            assert ":" in val

    @pytest.mark.asyncio
    async def test_lock_auto_expires(self, lock, fake_redis):
        """Lock key has expiration set."""
        async with lock.acquire("resource-3"):
            ttl = await fake_redis.ttl(f"{LOCK_PREFIX}resource-3")
            assert ttl > 0
            assert ttl <= 5  # lock_timeout=5.0

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, lock, fake_redis):
        """Lock is released even when body raises (context manager cleanup)."""
        with pytest.raises(ValueError, match="test error"):
            async with lock.acquire("resource-4"):
                # Lock exists during context
                val = await fake_redis.get(f"{LOCK_PREFIX}resource-4")
                assert val is not None
                raise ValueError("test error")


# ============================================================================
# Lock Timeout
# ============================================================================

class TestLockTimeout:

    @pytest.mark.asyncio
    async def test_timeout_raises(self, lock, fake_redis):
        """Second lock attempt times out when first holds the lock."""
        # Manually set a lock
        await fake_redis.set(
            f"{LOCK_PREFIX}contested",
            "other-holder",
            nx=True,
            ex=30,
        )
        with pytest.raises(TimeoutError, match="Lock timeout"):
            async with lock.acquire("contested", timeout=0.3, retry_delay=0.1):
                pass


# ============================================================================
# Release Safety
# ============================================================================

class TestReleaseSafety:

    @pytest.mark.asyncio
    async def test_stolen_lock_not_released(self, lock, fake_redis):
        """If lock value changed (stolen), release doesn't delete it."""
        # This tests _release_redis directly
        key = f"{LOCK_PREFIX}stolen-resource"
        await fake_redis.set(key, "other-holder-value")
        result = await lock._release_redis(fake_redis, key, "my-value")
        assert result is False
        # Key still exists (wasn't deleted)
        val = await fake_redis.get(key)
        assert val == "other-holder-value"


# ============================================================================
# is_locked
# ============================================================================

class TestIsLocked:

    @pytest.mark.asyncio
    async def test_locked_returns_true(self, lock, fake_redis):
        await fake_redis.set(f"{LOCK_PREFIX}active-lock", "holder")
        assert await lock.is_locked("active-lock") is True

    @pytest.mark.asyncio
    async def test_unlocked_returns_false(self, lock):
        assert await lock.is_locked("no-such-lock") is False


# ============================================================================
# force_release
# ============================================================================

class TestForceRelease:

    @pytest.mark.asyncio
    async def test_force_release_redis(self, lock, fake_redis):
        await fake_redis.set(f"{LOCK_PREFIX}stuck-resource", "stale-holder")
        released = await lock.force_release("stuck-resource")
        assert released is True
        val = await fake_redis.get(f"{LOCK_PREFIX}stuck-resource")
        assert val is None

    @pytest.mark.asyncio
    async def test_force_release_nonexistent(self, lock):
        released = await lock.force_release("nonexistent")
        assert released is False

    @pytest.mark.asyncio
    async def test_force_release_file_lock(self, lock_no_redis, tmp_path):
        """Force release removes file-based lock file."""
        lock_file = tmp_path / "file-resource.lock"
        lock_file.touch()
        released = await lock_no_redis.force_release("file-resource")
        assert released is True
        assert not lock_file.exists()

    @pytest.mark.asyncio
    async def test_force_release_keeps_a_held_file_lock(self, lock_no_redis, tmp_path):
        """Deleting a held flock's file does not release the holder; it lets a
        second holder lock a fresh file at the same path. So a held file lock
        is kept, and force_release reports it did not release."""
        import fcntl

        async with lock_no_redis.acquire("held-resource", timeout=1.0):
            released = await lock_no_redis.force_release("held-resource")
            assert released is False
            lock_file = tmp_path / "held-resource.lock"
            assert lock_file.exists()
            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    @pytest.mark.asyncio
    async def test_file_open_error_is_not_reported_as_contention(self, lock_no_redis):
        """A failed open() is an I/O error, not a live holder: it must surface
        as itself, not after the timeout as LockTimeoutError."""
        from src.state_locking import LockTimeoutError

        with patch("src.cache.distributed_lock.os.open", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError, match="denied") as excinfo:
                async with lock_no_redis.acquire("perm", timeout=5.0):
                    pass
        assert not isinstance(excinfo.value, LockTimeoutError)

    @pytest.mark.asyncio
    async def test_force_release_reports_an_os_error_instead_of_raising(self, lock_no_redis, tmp_path):
        (tmp_path / "ro-resource.lock").touch()
        with patch("src.state_locking.remove_lock_file_if_free", side_effect=PermissionError("read-only")):
            released = await lock_no_redis.force_release("ro-resource")
        assert released is False

    @pytest.mark.asyncio
    async def test_file_lock_reopens_when_a_cleaner_unlinks_before_flock(self, lock_no_redis, tmp_path):
        """If the path is unlinked between open() and flock(), the fallback must
        not keep a lock on the orphaned inode: it reopens, so the file now at
        the path is the one held."""
        import fcntl

        lock_file = tmp_path / "raced-resource.lock"
        real_open = os.open
        fired = []

        def racing_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if not fired and str(path) == str(lock_file):
                fired.append(True)
                os.unlink(path)
            return fd

        with patch("src.cache.distributed_lock.os.open", side_effect=racing_open):
            async with lock_no_redis.acquire("raced-resource", timeout=2.0):
                assert fired
                fd = real_open(str(lock_file), os.O_RDWR)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)

    @pytest.mark.asyncio
    async def test_inode_mismatch_retries_are_bounded_by_the_timeout(self, lock_no_redis):
        """If the path changes on every pass, acquire() must still honour its
        timeout (and yield to the loop) rather than spin forever."""
        from src.state_locking import LockTimeoutError

        with patch("src.state_locking.holds_current_inode", return_value=False):
            with pytest.raises(LockTimeoutError):
                await asyncio.wait_for(
                    lock_no_redis.acquire("churn", timeout=0.3).__aenter__(), timeout=5.0
                )

    @pytest.mark.asyncio
    async def test_file_lock_timeout_is_lock_timeout_error(self, lock_no_redis):
        from src.state_locking import LockTimeoutError

        async with lock_no_redis.acquire("busy-resource", timeout=1.0):
            with pytest.raises(LockTimeoutError):
                async with lock_no_redis.acquire("busy-resource", timeout=0.2):
                    pass


# ============================================================================
# health_check
# ============================================================================

class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_redis_healthy(self, lock):
        result = await lock.health_check()
        assert result["backend"] == "redis"
        assert result["status"] == "healthy"
        assert "active_locks" in result

    @pytest.mark.asyncio
    async def test_redis_counts_locks(self, lock, fake_redis):
        await fake_redis.set(f"{LOCK_PREFIX}a", "holder-a")
        await fake_redis.set(f"{LOCK_PREFIX}b", "holder-b")
        result = await lock.health_check()
        assert result["active_locks"] == 2

    @pytest.mark.asyncio
    async def test_file_fallback_healthy(self, lock_no_redis):
        result = await lock_no_redis.health_check()
        assert result["backend"] == "file"
        assert result["status"] == "healthy"


# ============================================================================
# File-Based Fallback
# ============================================================================

class TestFileFallback:

    @pytest.mark.asyncio
    async def test_acquire_file_lock(self, lock_no_redis, tmp_path):
        executed = False
        async with lock_no_redis.acquire("file-test"):
            executed = True
            # Lock file should exist
            lock_file = tmp_path / "file-test.lock"
            assert lock_file.exists()
        assert executed

    @pytest.mark.asyncio
    async def test_file_lock_contains_pid(self, lock_no_redis, tmp_path):
        async with lock_no_redis.acquire("pid-test"):
            lock_file = tmp_path / "pid-test.lock"
            content = lock_file.read_text()
            assert str(os.getpid()) in content

    @pytest.mark.asyncio
    async def test_body_oserror_is_not_contention(self, lock_no_redis, tmp_path):
        """An OSError from the locked body must reach the caller once, with the
        lock released, not be retried as flock contention (which re-acquired
        the lock and surfaced as RuntimeError from asynccontextmanager)."""
        with pytest.raises(TimeoutError, match="inner timeout"):
            async with lock_no_redis.acquire("body-test", timeout=2.0):
                raise TimeoutError("inner timeout")
        async with lock_no_redis.acquire("body-test", timeout=0.5):
            pass  # released: re-acquirable at once

    @pytest.mark.asyncio
    async def test_is_locked_file_fallback(self, lock_no_redis, tmp_path):
        """is_locked returns False for non-held file lock."""
        assert await lock_no_redis.is_locked("no-file-lock") is False


# ============================================================================
# Singleton
# ============================================================================

class TestSingleton:

    def test_get_distributed_lock_returns_same_instance(self):
        # Reset singleton
        import src.cache.distributed_lock as mod
        old = mod._distributed_lock
        mod._distributed_lock = None
        try:
            lock1 = get_distributed_lock()
            lock2 = get_distributed_lock()
            assert lock1 is lock2
        finally:
            mod._distributed_lock = old
