"""
Redis-based rate limiter with sliding window support.

Provides fast, distributed rate limiting using Redis.
Falls back to PostgreSQL if Redis is unavailable.

Usage:
    from src.cache import get_rate_limiter
    
    limiter = get_rate_limiter()
    if await limiter.check("agent-123", limit=20, window=3600):
        # Proceed with operation
        await limiter.record("agent-123", window=3600)
    else:
        raise RateLimitExceeded()
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from .redis_client import get_redis
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Key prefix for rate limits
RATE_LIMIT_PREFIX = "rate_limit:"


class RateLimiter:
    """
    Redis-based rate limiter with sliding window.
    
    Uses Redis sorted sets (ZSET) for efficient sliding window tracking.
    Falls back to PostgreSQL if Redis is unavailable.
    """

    @staticmethod
    def _member() -> str:
        # One member per operation. A member built from whole seconds and the
        # singleton's id() made every operation in the same second overwrite
        # the previous one, so ZCARD counted busy seconds, not operations.
        return f"{time.time_ns()}:{uuid.uuid4().hex}"

    async def check(
        self,
        resource_id: str,
        limit: int,
        window: int,
        *,
        operation: str = "default",
    ) -> bool:
        """
        Check if resource is within rate limit.
        
        Args:
            resource_id: Unique identifier (e.g., agent_id)
            limit: Maximum number of operations allowed
            window: Time window in seconds
            operation: Operation type (e.g., "kg_store", "tool_call")
        
        Returns:
            True if within limit, False if exceeded

        Not an enforcement primitive: it answers True with no Redis and when
        Redis errors, and a separate record() leaves a check-then-act gap. The
        knowledge store budget (src/storage/kg_write_budget.py) enforces in
        PostgreSQL for that reason.
        """
        redis = await get_redis()
        if redis is None:
            return True
        
        key = f"{RATE_LIMIT_PREFIX}{operation}:{resource_id}"
        now = int(time.time())
        window_start = now - window
        
        try:
            # Remove expired entries (older than window)
            await redis.zremrangebyscore(key, 0, window_start)
            
            # Count current entries in window
            count = await redis.zcard(key)
            
            if count >= limit:
                logger.debug(
                    f"Rate limit exceeded: {resource_id} has {count}/{limit} "
                    f"operations in last {window}s"
                )
                return False
            
            return True
        except Exception as e:
            logger.warning(f"Redis rate limit check failed: {e} - allowing operation")
            return True  # Fail open

    async def record(
        self,
        resource_id: str,
        window: int,
        *,
        operation: str = "default",
    ) -> None:
        """
        Record an operation for rate limiting.
        
        Args:
            resource_id: Unique identifier (e.g., agent_id)
            window: Time window in seconds (for TTL)
            operation: Operation type (e.g., "kg_store", "tool_call")
        """
        redis = await get_redis()
        if redis is None:
            # Fallback: PostgreSQL backend will record
            return
        
        key = f"{RATE_LIMIT_PREFIX}{operation}:{resource_id}"
        now = int(time.time())
        
        try:
            await redis.zadd(key, {self._member(): now})
            
            # Set expiration to window + buffer (cleanup safety margin)
            await redis.expire(key, window + 60)
            
            logger.debug(f"Recorded rate limit operation: {resource_id} ({operation})")
        except Exception as e:
            logger.warning(f"Redis rate limit record failed: {e}")

    async def get_count(
        self,
        resource_id: str,
        window: int,
        *,
        operation: str = "default",
    ) -> int:
        """
        Get current operation count for resource.
        
        Args:
            resource_id: Unique identifier
            window: Time window in seconds
            operation: Operation type
        
        Returns:
            Current count of operations in window
        """
        redis = await get_redis()
        if redis is None:
            return 0
        
        key = f"{RATE_LIMIT_PREFIX}{operation}:{resource_id}"
        now = int(time.time())
        window_start = now - window
        
        try:
            # Remove expired entries
            await redis.zremrangebyscore(key, 0, window_start)
            
            # Count remaining entries
            return await redis.zcard(key)
        except Exception as e:
            logger.warning(f"Redis rate limit count failed: {e}")
            return 0

    async def reset(
        self,
        resource_id: str,
        *,
        operation: str = "default",
    ) -> None:
        """
        Reset rate limit for resource (for testing/admin).
        
        Args:
            resource_id: Unique identifier
            operation: Operation type
        """
        redis = await get_redis()
        if redis is None:
            return
        
        key = f"{RATE_LIMIT_PREFIX}{operation}:{resource_id}"
        try:
            await redis.delete(key)
            logger.debug(f"Reset rate limit: {resource_id} ({operation})")
        except Exception as e:
            logger.warning(f"Redis rate limit reset failed: {e}")


# Singleton instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get singleton rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter

