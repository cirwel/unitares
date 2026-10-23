"""
Tests for src/cache/rate_limiter.py (Redis-backed sliding window rate limiter).

Note: tests/test_rate_limiter.py tests the OLD in-memory module at src/rate_limiter.py.
This file tests the NEW Redis-backed version at src/cache/rate_limiter.py.

Covers:
- check() - within limit, exceeded, no redis fallback
- record() - adds entries, sets TTL
- get_count() - counts entries in window
- reset() - clears rate limit
- Sliding window expiration behavior
- Singleton get_rate_limiter()
"""

import time
import pytest
from unittest.mock import patch

fakeredis = pytest.importorskip("fakeredis", reason="fakeredis not installed")
import fakeredis.aioredis

from src.cache.rate_limiter import (
    RateLimiter,
    get_rate_limiter,
    RATE_LIMIT_PREFIX,
)


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def limiter(fake_redis):
    """RateLimiter with fakeredis backend."""
    async def _get_fake_redis():
        return fake_redis

    with patch("src.cache.rate_limiter.get_redis", new=_get_fake_redis):
        yield RateLimiter()


@pytest.fixture
def limiter_no_redis():
    """RateLimiter with no Redis (fallback behavior)."""
    async def _get_none():
        return None

    with patch("src.cache.rate_limiter.get_redis", new=_get_none):
        yield RateLimiter()


# ============================================================================
# check()
# ============================================================================

class TestCheck:

    @pytest.mark.asyncio
    async def test_within_limit(self, limiter):
        result = await limiter.check("agent-1", limit=10, window=3600)
        assert result is True

    @pytest.mark.asyncio
    async def test_exceeded_limit(self, limiter, fake_redis):
        """After recording enough operations, check returns False."""
        key = f"{RATE_LIMIT_PREFIX}default:agent-2"
        now = int(time.time())
        # Add 5 entries (limit will be 5)
        for i in range(5):
            await fake_redis.zadd(key, {f"{now}:{i}": now})

        result = await limiter.check("agent-2", limit=5, window=3600)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_redis_allows(self, limiter_no_redis):
        """No Redis = fail open (allow operation)."""
        result = await limiter_no_redis.check("agent-3", limit=1, window=60)
        assert result is True

    @pytest.mark.asyncio
    async def test_custom_operation(self, limiter, fake_redis):
        """Operation type is included in the key."""
        key = f"{RATE_LIMIT_PREFIX}kg_store:agent-4"
        now = int(time.time())
        for i in range(3):
            await fake_redis.zadd(key, {f"{now}:{i}": now})

        # Exceeded for kg_store
        result = await limiter.check("agent-4", limit=3, window=3600, operation="kg_store")
        assert result is False

        # But default operation is still within limit
        result = await limiter.check("agent-4", limit=3, window=3600, operation="default")
        assert result is True

    @pytest.mark.asyncio
    async def test_expired_entries_removed(self, limiter, fake_redis):
        """Entries outside the window are cleaned up during check."""
        key = f"{RATE_LIMIT_PREFIX}default:agent-5"
        now = int(time.time())
        old_time = now - 7200  # 2 hours ago
        # Add old entries (outside 1hr window)
        for i in range(10):
            await fake_redis.zadd(key, {f"{old_time}:{i}": old_time})

        # Should be within limit because old entries are cleaned
        result = await limiter.check("agent-5", limit=5, window=3600)
        assert result is True


# ============================================================================
# record()
# ============================================================================

class TestRecord:

    @pytest.mark.asyncio
    async def test_record_adds_entry(self, limiter, fake_redis):
        await limiter.record("agent-10", window=3600)
        key = f"{RATE_LIMIT_PREFIX}default:agent-10"
        count = await fake_redis.zcard(key)
        assert count == 1

    @pytest.mark.asyncio
    async def test_record_sets_ttl(self, limiter, fake_redis):
        await limiter.record("agent-11", window=3600)
        key = f"{RATE_LIMIT_PREFIX}default:agent-11"
        ttl = await fake_redis.ttl(key)
        assert ttl > 3600  # window + 60 buffer
        assert ttl <= 3660

    @pytest.mark.asyncio
    async def test_record_multiple_different_seconds(self, limiter, fake_redis):
        """Multiple records at different times create distinct entries."""
        key = f"{RATE_LIMIT_PREFIX}default:agent-12"
        now = int(time.time())
        # Simulate records at different timestamps by inserting directly
        for i in range(3):
            ts = now - i  # different seconds
            await fake_redis.zadd(key, {f"{ts}:{id(limiter)}": ts})
        count = await fake_redis.zcard(key)
        assert count == 3

    @pytest.mark.asyncio
    async def test_record_no_redis(self, limiter_no_redis):
        """No Redis = silently skip (no error)."""
        await limiter_no_redis.record("agent-13", window=3600)
        # Just verify it doesn't raise


# ============================================================================
# get_count()
# ============================================================================

class TestGetCount:

    @pytest.mark.asyncio
    async def test_count_empty(self, limiter):
        count = await limiter.get_count("agent-20", window=3600)
        assert count == 0

    @pytest.mark.asyncio
    async def test_count_after_records(self, limiter, fake_redis):
        """Count reflects distinct entries in window."""
        key = f"{RATE_LIMIT_PREFIX}default:agent-21"
        now = int(time.time())
        # Insert distinct entries (different timestamps)
        for i in range(4):
            await fake_redis.zadd(key, {f"{now - i}:{id(limiter)}": now - i})
            await fake_redis.expire(key, 3660)
        count = await limiter.get_count("agent-21", window=3600)
        assert count == 4

    @pytest.mark.asyncio
    async def test_count_excludes_expired(self, limiter, fake_redis):
        """Old entries are removed before counting."""
        key = f"{RATE_LIMIT_PREFIX}default:agent-22"
        now = int(time.time())
        old_time = now - 7200
        # Add old entries
        for i in range(5):
            await fake_redis.zadd(key, {f"{old_time}:{i}": old_time})
        # Add recent entry
        await fake_redis.zadd(key, {f"{now}:recent": now})

        count = await limiter.get_count("agent-22", window=3600)
        assert count == 1  # Only the recent one

    @pytest.mark.asyncio
    async def test_count_no_redis(self, limiter_no_redis):
        count = await limiter_no_redis.get_count("agent-23", window=3600)
        assert count == 0


# ============================================================================
# reset()
# ============================================================================

class TestReset:

    @pytest.mark.asyncio
    async def test_reset_clears(self, limiter, fake_redis):
        key = f"{RATE_LIMIT_PREFIX}default:agent-30"
        now = int(time.time())
        # Insert distinct entries
        for i in range(2):
            await fake_redis.zadd(key, {f"{now - i}:x": now - i})
            await fake_redis.expire(key, 3660)
        assert await limiter.get_count("agent-30", window=3600) == 2

        await limiter.reset("agent-30")
        assert await limiter.get_count("agent-30", window=3600) == 0

    @pytest.mark.asyncio
    async def test_reset_nonexistent(self, limiter):
        """Reset on nonexistent key doesn't raise."""
        await limiter.reset("nonexistent-agent")

    @pytest.mark.asyncio
    async def test_reset_no_redis(self, limiter_no_redis):
        await limiter_no_redis.reset("agent-31")
        # Just verify it doesn't raise


# ============================================================================
# Integration: check + record flow
# ============================================================================

class TestCheckRecordFlow:

    @pytest.mark.asyncio
    async def test_check_then_record_then_exceed(self, limiter, fake_redis):
        """Full flow: pre-populate entries, then check exceeds limit."""
        agent = "flow-agent"
        limit = 3
        window = 3600
        key = f"{RATE_LIMIT_PREFIX}default:{agent}"
        now = int(time.time())

        # Pre-populate with distinct entries at different timestamps
        for i in range(limit):
            await fake_redis.zadd(key, {f"{now - i}:op{i}": now - i})
            await fake_redis.expire(key, 3660)

        # Now should be at limit
        assert await limiter.check(agent, limit=limit, window=window) is False

        # Below limit should pass
        assert await limiter.check(agent, limit=limit + 1, window=window) is True


# ============================================================================
# Singleton
# ============================================================================

class TestSingleton:

    def test_get_rate_limiter_returns_same_instance(self):
        import src.cache.rate_limiter as mod
        old = mod._rate_limiter
        mod._rate_limiter = None
        try:
            rl1 = get_rate_limiter()
            rl2 = get_rate_limiter()
            assert rl1 is rl2
        finally:
            mod._rate_limiter = old


# ============================================================================
# record(): one member per operation
# ============================================================================

class TestRecordUniqueness:

    @pytest.mark.asyncio
    async def test_record_counts_two_writes_in_one_second(self, limiter, fake_redis):
        """Regression: members were f"{int(now)}:{id(self)}" on a singleton, so
        every write in one second overwrote the last."""
        with patch("src.cache.rate_limiter.time.time", return_value=1_800_000_000.0):
            await limiter.record("agent-5", 3600)
            await limiter.record("agent-5", 3600)
        assert await fake_redis.zcard(f"{RATE_LIMIT_PREFIX}default:agent-5") == 2
