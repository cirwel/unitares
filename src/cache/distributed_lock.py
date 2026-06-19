"""
Distributed locking for multi-server deployments.

Provides Redis-based locks that work across multiple server instances.
Falls back to file-based locking (fcntl) for single-server mode.

Redis lock implementation uses SETNX with automatic expiration
to prevent deadlocks from crashed processes.
"""

from __future__ import annotations

import os
import time
import asyncio
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from .redis_client import get_redis
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Key prefix for distributed locks
LOCK_PREFIX = "lock:"

# Default lock timeout (auto-release if holder crashes)
DEFAULT_LOCK_TIMEOUT = 30.0  # seconds

# Retry settings
DEFAULT_RETRY_DELAY = 0.1  # seconds
DEFAULT_MAX_RETRIES = 50  # 50 * 0.1s = 5s max wait


class DistributedLock:
    """
    Distributed lock with Redis backend and file-based fallback.

    Uses Redis SETNX with expiration for distributed locking.
    Falls back to fcntl file locking when Redis is unavailable.
    """

    def __init__(
        self,
        lock_dir: Optional[Path] = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ):
        """
        Initialize distributed lock manager.

        Args:
            lock_dir: Directory for file-based fallback locks
            lock_timeout: Auto-release timeout for Redis locks (seconds)
        """
        if lock_dir is None:
            project_root = Path(__file__).parent.parent.parent
            lock_dir = project_root / "data" / "locks"
        self.lock_dir = lock_dir
        self._ensure_lock_dir()
        self.lock_timeout = lock_timeout

        # Track active file locks (for fallback cleanup)
        self._file_locks: Dict[str, int] = {}  # resource_id -> fd

    def _ensure_lock_dir(self) -> None:
        """Ensure the file-fallback lock directory exists.

        Called at construction AND before each file-fallback acquisition. The
        lock directory is gitignored and can be removed out from under a
        long-running process (e.g. a ``git worktree remove``/``add`` rebuild of
        a deploy worktree, ``git clean -fdx``, or a manual ``rm``), after which
        ``os.open(lock_file, O_CREAT)`` would raise ENOENT forever. Re-creating
        here makes the fallback self-healing; ``exist_ok=True`` is a cheap stat
        when the directory is already present.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def acquire(
        self,
        resource_id: str,
        *,
        timeout: float = 5.0,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """
        Acquire distributed lock on a resource.

        Args:
            resource_id: Unique identifier for the resource (e.g., agent_id)
            timeout: Maximum time to wait for lock (seconds)
            retry_delay: Delay between retry attempts (seconds)

        Raises:
            TimeoutError: If lock cannot be acquired within timeout

        Usage:
            async with lock.acquire("agent-123"):
                # exclusive access to agent-123
                ...
        """
        # Try Redis first
        redis = await get_redis()
        if redis is not None:
            async with self._acquire_redis(redis, resource_id, timeout, retry_delay):
                yield
            return

        # Fallback to file-based locking
        async with self._acquire_file(resource_id, timeout, retry_delay):
            yield

    @asynccontextmanager
    async def _acquire_redis(
        self,
        redis,
        resource_id: str,
        timeout: float,
        retry_delay: float,
    ):
        """Acquire lock using Redis SETNX with expiration."""
        key = f"{LOCK_PREFIX}{resource_id}"
        lock_value = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        start_time = time.monotonic()

        while True:
            # Try to acquire lock with SETNX + expiration
            acquired = await redis.set(
                key,
                lock_value,
                nx=True,  # Only set if not exists
                ex=int(self.lock_timeout),  # Auto-expire
            )

            if acquired:
                logger.debug(f"Redis lock acquired: {resource_id}")
                try:
                    yield
                finally:
                    # Release lock (only if we still hold it)
                    await self._release_redis(redis, key, lock_value)
                return

            # Check timeout
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                # Get lock holder info for error message
                holder = await redis.get(key)
                raise TimeoutError(
                    f"Lock timeout for '{resource_id}' after {timeout:.1f}s. "
                    f"Held by: {holder}"
                )

            # Wait and retry
            await asyncio.sleep(retry_delay)

    async def _release_redis(self, redis, key: str, lock_value: str) -> bool:
        """
        Release Redis lock only if we still hold it.

        Uses Lua script for atomic check-and-delete.
        """
        # Lua script: delete key only if value matches
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            result = await redis.eval(script, 1, key, lock_value)
            if result:
                logger.debug(f"Redis lock released: {key}")
            else:
                logger.warning(f"Redis lock stolen or expired: {key}")
            return bool(result)
        except Exception as e:
            logger.warning(f"Redis lock release failed: {e}")
            return False

    @asynccontextmanager
    async def _acquire_file(
        self,
        resource_id: str,
        timeout: float,
        retry_delay: float,
    ):
        """Fallback to file-based locking (fcntl)."""
        import fcntl

        # Re-create the lock dir if it was removed since construction.
        self._ensure_lock_dir()
        lock_file = self.lock_dir / f"{resource_id}.lock"
        fd = None
        start_time = time.monotonic()

        try:
            while True:
                try:
                    # Open lock file
                    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)

                    # Try non-blocking lock
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                    # Write PID to lock file
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}".encode())
                    os.fsync(fd)

                    logger.debug(f"File lock acquired: {resource_id}")
                    self._file_locks[resource_id] = fd
                    yield
                    return

                except IOError:
                    # Lock is held, close our fd and retry
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fd = None

                    # Check timeout
                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout:
                        raise TimeoutError(
                            f"File lock timeout for '{resource_id}' after {timeout:.1f}s"
                        )

                    await asyncio.sleep(retry_delay)

        finally:
            # Release lock
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._file_locks.pop(resource_id, None)
                logger.debug(f"File lock released: {resource_id}")

    async def is_locked(self, resource_id: str) -> bool:
        """
        Check if resource is currently locked (non-blocking).

        Note: This is a point-in-time check; the lock state may change
        immediately after this returns.
        """
        redis = await get_redis()
        if redis is not None:
            try:
                key = f"{LOCK_PREFIX}{resource_id}"
                return await redis.exists(key) > 0
            except Exception:
                pass

        # Check file-based lock
        lock_file = self.lock_dir / f"{resource_id}.lock"
        if not lock_file.exists():
            return False

        try:
            import fcntl
            fd = os.open(str(lock_file), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False  # Could acquire = not locked
            except IOError:
                return True  # Couldn't acquire = locked
            finally:
                os.close(fd)
        except (OSError, IOError):
            return False

    async def force_release(self, resource_id: str) -> bool:
        """
        Force-release a lock (use with caution!).

        Only use for recovering from stuck locks when you're certain
        no process is actively using the resource.
        """
        released = False

        # Try Redis
        redis = await get_redis()
        if redis is not None:
            try:
                key = f"{LOCK_PREFIX}{resource_id}"
                result = await redis.delete(key)
                if result:
                    released = True
                    logger.warning(f"Force-released Redis lock: {resource_id}")
            except Exception as e:
                logger.warning(f"Failed to force-release Redis lock: {e}")

        # Try file-based
        lock_file = self.lock_dir / f"{resource_id}.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
                released = True
                logger.warning(f"Force-released file lock: {resource_id}")
            except OSError as e:
                logger.warning(f"Failed to force-release file lock: {e}")

        return released

    async def health_check(self) -> Dict[str, Any]:
        """Get lock system health status."""
        redis = await get_redis()
        if redis is not None:
            try:
                await redis.ping()
                # Count lock keys
                cursor = 0
                count = 0
                while True:
                    cursor, keys = await redis.scan(
                        cursor, match=f"{LOCK_PREFIX}*", count=100
                    )
                    count += len(keys)
                    if cursor == 0:
                        break
                return {
                    "backend": "redis",
                    "status": "healthy",
                    "active_locks": count,
                }
            except Exception as e:
                return {
                    "backend": "redis",
                    "status": "error",
                    "error": str(e),
                }

        # Count file-based locks
        lock_files = list(self.lock_dir.glob("*.lock"))
        return {
            "backend": "file",
            "status": "healthy",
            "active_locks": len(lock_files),
            "lock_dir": str(self.lock_dir),
        }


# Singleton instance
_distributed_lock: Optional[DistributedLock] = None


def get_distributed_lock() -> DistributedLock:
    """Get singleton distributed lock instance."""
    global _distributed_lock
    if _distributed_lock is None:
        _distributed_lock = DistributedLock()
    return _distributed_lock
