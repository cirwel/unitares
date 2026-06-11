"""
Redis client with production-grade resilience.

Features:
- Circuit breaker: Stops hammering Redis when it's down
- Connection pooling: Efficient connection management
- Retry with backoff: Handles transient failures gracefully
- Periodic health check: Reduces overhead vs ping-per-call
- Fallback metrics: Visibility into degradation events
- Sentinel support: High availability deployments

Environment variables:
- REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)
- REDIS_ENABLED: Set to "0" to disable Redis entirely
- REDIS_SENTINEL_HOSTS: Comma-separated sentinel hosts (e.g., "host1:26379,host2:26379")
- REDIS_SENTINEL_MASTER: Sentinel master name (default: "mymaster")
- REDIS_POOL_SIZE: Connection pool size (default: 10)
- REDIS_RETRY_ATTEMPTS: Max retry attempts (default: 3)
- REDIS_CIRCUIT_BREAKER_THRESHOLD: Failures before circuit opens (default: 5)
- REDIS_CIRCUIT_BREAKER_TIMEOUT: Seconds before retry after circuit opens (default: 30)
"""

from __future__ import annotations

import os
import time
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any, Dict, Callable, TypeVar
from functools import wraps
import json as _json
import threading
import inspect

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Type variable for generic retry decorator
T = TypeVar("T")


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class RedisConfig:
    """Redis configuration with sensible defaults."""
    url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    enabled: bool = field(default_factory=lambda: os.getenv("REDIS_ENABLED", "1").lower() not in ("0", "false", "no"))

    # Connection pool settings
    pool_size: int = field(default_factory=lambda: int(os.getenv("REDIS_POOL_SIZE", "10")))
    socket_timeout: float = 2.0
    socket_connect_timeout: float = 2.0

    # Retry settings
    retry_attempts: int = field(default_factory=lambda: int(os.getenv("REDIS_RETRY_ATTEMPTS", "3")))
    retry_base_delay: float = 0.1  # Base delay in seconds
    retry_max_delay: float = 2.0   # Max delay after backoff

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(default_factory=lambda: int(os.getenv("REDIS_CIRCUIT_BREAKER_THRESHOLD", "5")))
    circuit_breaker_timeout: float = field(default_factory=lambda: float(os.getenv("REDIS_CIRCUIT_BREAKER_TIMEOUT", "30")))

    # Sentinel settings (for HA)
    sentinel_hosts: Optional[str] = field(default_factory=lambda: os.getenv("REDIS_SENTINEL_HOSTS"))
    sentinel_master: str = field(default_factory=lambda: os.getenv("REDIS_SENTINEL_MASTER", "mymaster"))

    # Health check settings
    health_check_interval: float = 30.0  # Seconds between health checks


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Failing, requests are rejected immediately
    - HALF_OPEN: Testing if service recovered
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 5, timeout: float = 30.0):
        self.threshold = threshold
        self.timeout = timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()
        # Telemetry: ring buffer of trip timestamps (state transitions to OPEN)
        self._trip_timestamps: deque[datetime] = deque(maxlen=100)

    @property
    def state(self) -> str:
        """Get current circuit state, auto-transitioning if needed."""
        with self._lock:
            if self._state == self.OPEN:
                # Check if timeout has passed, transition to half-open
                if self._last_failure_time and (time.time() - self._last_failure_time) >= self.timeout:
                    self._state = self.HALF_OPEN
                    logger.info("Circuit breaker: OPEN -> HALF_OPEN (testing recovery)")
            return self._state

    def record_success(self) -> None:
        """Record successful operation, potentially closing circuit."""
        with self._lock:
            if self._state == self.HALF_OPEN:
                logger.info("Circuit breaker: HALF_OPEN -> CLOSED (service recovered)")
            self._state = self.CLOSED
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record failed operation, potentially opening circuit."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == self.HALF_OPEN:
                # Failed during test, back to open
                self._state = self.OPEN
                self._trip_timestamps.append(datetime.now(timezone.utc))
                logger.warning("Circuit breaker: HALF_OPEN -> OPEN (recovery failed)")
            elif self._failure_count >= self.threshold:
                if self._state != self.OPEN:
                    logger.warning(f"Circuit breaker: CLOSED -> OPEN (threshold {self.threshold} reached)")
                    self._trip_timestamps.append(datetime.now(timezone.utc))
                self._state = self.OPEN

    def is_available(self) -> bool:
        """Check if requests should be allowed through."""
        return self.state != self.OPEN

    def get_telemetry(self) -> Dict[str, Any]:
        """Return circuit breaker telemetry snapshot."""
        now = datetime.now(timezone.utc)
        with self._lock:
            trips = list(self._trip_timestamps)
            state = self._state
            failure_count = self._failure_count

        trips_1h = sum(1 for t in trips if (now - t).total_seconds() <= 3600)
        trips_24h = sum(1 for t in trips if (now - t).total_seconds() <= 86400)
        last_trip = trips[-1].isoformat() if trips else None

        return {
            "state": state,
            "trips_1h": trips_1h,
            "trips_24h": trips_24h,
            "last_trip": last_trip,
            "failure_count": failure_count,
        }

    def reset(self) -> None:
        """Reset circuit breaker to closed state."""
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._last_failure_time = None

    def snapshot_for_persist(self) -> str:
        """Serialize circuit breaker state to JSON for Redis storage."""
        with self._lock:
            return _json.dumps({
                "state": self._state,
                "failure_count": self._failure_count,
                "trip_timestamps": [t.isoformat() for t in self._trip_timestamps],
            })

    def restore_from_persist(self, raw: str) -> None:
        """Restore circuit breaker state from a JSON string."""
        try:
            data = _json.loads(raw)
            with self._lock:
                self._state = data.get("state", self.CLOSED)
                self._failure_count = data.get("failure_count", 0)
                for ts in data.get("trip_timestamps", []):
                    self._trip_timestamps.append(datetime.fromisoformat(ts))
            logger.info(f"Restored circuit breaker from Redis (state={self._state})")
        except Exception as e:
            logger.warning(f"Failed to restore circuit breaker from Redis: {e}")


# =============================================================================
# FALLBACK METRICS
# =============================================================================

METRICS_REDIS_KEY = "unitares:metrics:redis_client"
CB_REDIS_KEY = "unitares:metrics:circuit_breaker"


@dataclass
class RedisMetrics:
    """Metrics for Redis operations and fallback events."""

    # Operation counts
    operations_total: int = 0
    operations_success: int = 0
    operations_failed: int = 0
    operations_fallback: int = 0

    # Retry stats
    retries_total: int = 0
    retries_success: int = 0

    # Circuit breaker stats
    circuit_opens: int = 0
    circuit_half_opens: int = 0

    # Connection stats
    connections_created: int = 0
    connections_failed: int = 0
    reconnections: int = 0

    # Health check stats
    health_checks_total: int = 0
    health_checks_failed: int = 0
    last_health_check: Optional[float] = None
    last_healthy: Optional[float] = None

    # Timing
    _start_time: float = field(default_factory=time.time)

    # Persistence fields (not serialized to dict)
    _PERSIST_FIELDS: tuple = field(
        default=(
            "operations_total", "operations_success", "operations_failed",
            "operations_fallback", "retries_total", "retries_success",
            "circuit_opens", "circuit_half_opens", "connections_created",
            "connections_failed", "reconnections", "health_checks_total",
            "health_checks_failed",
        ),
        init=False, repr=False,
    )

    def snapshot_for_persist(self) -> str:
        """Serialize counter fields to JSON for Redis storage."""
        return _json.dumps({f: getattr(self, f) for f in self._PERSIST_FIELDS})

    def restore_from_persist(self, raw: str) -> None:
        """Restore counter fields from a JSON string."""
        try:
            data = _json.loads(raw)
            for f in self._PERSIST_FIELDS:
                if f in data:
                    setattr(self, f, data[f])
            logger.info(f"Restored metrics from Redis (ops_total={self.operations_total})")
        except Exception as e:
            logger.warning(f"Failed to restore metrics from Redis: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Export metrics as dict."""
        uptime = time.time() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "operations": {
                "total": self.operations_total,
                "success": self.operations_success,
                "failed": self.operations_failed,
                "fallback": self.operations_fallback,
                "success_rate": round(self.operations_success / max(self.operations_total, 1) * 100, 1),
            },
            "retries": {
                "total": self.retries_total,
                "success": self.retries_success,
            },
            "circuit_breaker": {
                "opens": self.circuit_opens,
                "half_opens": self.circuit_half_opens,
            },
            "connections": {
                "created": self.connections_created,
                "failed": self.connections_failed,
                "reconnections": self.reconnections,
            },
            "health": {
                "checks_total": self.health_checks_total,
                "checks_failed": self.health_checks_failed,
                "last_check": self.last_health_check,
                "last_healthy": self.last_healthy,
            },
        }


# =============================================================================
# REDIS CLIENT WITH RESILIENCE
# =============================================================================

class ResilientRedisClient:
    """
    Redis client with production-grade resilience features.

    Thread-safe singleton with:
    - Circuit breaker for fast failure
    - Connection pooling for efficiency
    - Retry with exponential backoff
    - Periodic health checks
    - Comprehensive metrics
    """

    def __init__(self, config: Optional[RedisConfig] = None):
        self.config = config or RedisConfig()
        self._redis: Optional[Any] = None
        self._redis_module: Optional[Any] = None
        self._available: Optional[bool] = None
        self._lock = asyncio.Lock()
        self._init_lock = threading.Lock()
        self._metrics_restored = False

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            threshold=self.config.circuit_breaker_threshold,
            timeout=self.config.circuit_breaker_timeout,
        )

        # Metrics
        self.metrics = RedisMetrics()

        # Health check task
        self._health_check_task: Optional[asyncio.Task] = None
        self._shutdown = False

    def _get_redis_module(self):
        """Lazy import of redis module."""
        if self._redis_module is None:
            with self._init_lock:
                if self._redis_module is None:
                    try:
                        import redis.asyncio as redis
                        self._redis_module = redis
                    except ImportError:
                        logger.warning("redis package not installed - using fallback mode")
                        self._redis_module = False
        return self._redis_module if self._redis_module else None

    async def _create_connection(self) -> Optional[Any]:
        """Create Redis connection with pooling and optional Sentinel support."""
        redis_mod = self._get_redis_module()
        if redis_mod is None:
            return None

        try:
            # Check for Sentinel configuration
            if self.config.sentinel_hosts:
                return await self._create_sentinel_connection(redis_mod)

            # Standard connection with pool
            self._redis = redis_mod.from_url(
                self.config.url,
                decode_responses=True,
                socket_connect_timeout=self.config.socket_connect_timeout,
                socket_timeout=self.config.socket_timeout,
                max_connections=self.config.pool_size,
                health_check_interval=30,  # Built-in health check
            )

            # Verify connection
            await self._redis.ping()
            self.metrics.connections_created += 1
            self._available = True
            logger.info(f"Redis connected: {self.config.url} (pool_size={self.config.pool_size})")

            # Restore persisted metrics only on first successful connection
            if not self._metrics_restored:
                await self._restore_metrics()
                self._metrics_restored = True

            return self._redis

        except Exception as e:
            self.metrics.connections_failed += 1
            logger.warning(f"Redis connection failed: {e}")
            self._available = False
            return None

    async def _create_sentinel_connection(self, redis_mod) -> Optional[Any]:
        """Create connection through Redis Sentinel for HA."""
        try:
            # Parse sentinel hosts
            sentinel_hosts = []
            for host_port in self.config.sentinel_hosts.split(","):
                host_port = host_port.strip()
                if ":" in host_port:
                    host, port = host_port.rsplit(":", 1)
                    sentinel_hosts.append((host, int(port)))
                else:
                    sentinel_hosts.append((host_port, 26379))

            # Create Sentinel client
            sentinel = redis_mod.Sentinel(
                sentinel_hosts,
                socket_timeout=self.config.socket_timeout,
                socket_connect_timeout=self.config.socket_connect_timeout,
            )

            # Get master connection
            self._redis = sentinel.master_for(
                self.config.sentinel_master,
                decode_responses=True,
            )

            # Verify connection
            await self._redis.ping()
            self.metrics.connections_created += 1
            self._available = True
            logger.info(f"Redis Sentinel connected: master={self.config.sentinel_master}, sentinels={sentinel_hosts}")
            return self._redis

        except Exception as e:
            self.metrics.connections_failed += 1
            logger.warning(f"Redis Sentinel connection failed: {e}")
            self._available = False
            return None

    async def get(self) -> Optional[Any]:
        """
        Get Redis client instance with resilience checks.

        Returns:
            Redis client if available, None if disabled/unavailable.
        """
        # Check if explicitly disabled
        if not self.config.enabled:
            return None

        # Check circuit breaker
        if not self.circuit_breaker.is_available():
            self.metrics.operations_fallback += 1
            return None

        # Return existing healthy connection
        if self._redis is not None and self._available:
            return self._redis

        # Create new connection (with lock to prevent race)
        async with self._lock:
            # Double-check after acquiring lock
            if self._redis is not None and self._available:
                return self._redis

            # Try to connect with retry
            for attempt in range(self.config.retry_attempts):
                try:
                    result = await self._create_connection()
                    if result is not None:
                        self.circuit_breaker.record_success()
                        # Start health check task if not running
                        self._ensure_health_check_running()
                        return result
                except Exception as e:
                    self.metrics.retries_total += 1
                    if attempt < self.config.retry_attempts - 1:
                        delay = min(
                            self.config.retry_base_delay * (2 ** attempt),
                            self.config.retry_max_delay
                        )
                        logger.debug(f"Redis connection attempt {attempt + 1} failed, retrying in {delay:.1f}s: {e}")
                        await asyncio.sleep(delay)

            # All attempts failed
            self.circuit_breaker.record_failure()
            self._available = False
            return None

    async def execute_with_retry(
        self,
        operation: Callable,
        *args,
        fallback: Optional[Callable] = None,
        **kwargs,
    ) -> Any:
        """
        Execute Redis operation with retry and fallback.

        Args:
            operation: Async callable to execute
            fallback: Optional fallback callable if Redis fails
            *args, **kwargs: Arguments to pass to operation

        Returns:
            Operation result, or fallback result if Redis fails
        """
        self.metrics.operations_total += 1

        # Check circuit breaker first
        if not self.circuit_breaker.is_available():
            self.metrics.operations_fallback += 1
            if fallback:
                return await fallback(*args, **kwargs) if inspect.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
            return None

        redis = await self.get()
        if redis is None:
            self.metrics.operations_fallback += 1
            if fallback:
                return await fallback(*args, **kwargs) if inspect.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
            return None

        last_error = None
        for attempt in range(self.config.retry_attempts):
            try:
                result = await operation(redis, *args, **kwargs)
                self.metrics.operations_success += 1
                self.circuit_breaker.record_success()
                if attempt > 0:
                    self.metrics.retries_success += 1
                return result
            except Exception as e:
                last_error = e
                self.metrics.retries_total += 1

                # Check if connection is dead
                if self._is_connection_error(e):
                    self._available = False
                    self._redis = None
                    self.metrics.reconnections += 1
                    redis = await self.get()
                    if redis is None:
                        break

                if attempt < self.config.retry_attempts - 1:
                    delay = min(
                        self.config.retry_base_delay * (2 ** attempt),
                        self.config.retry_max_delay
                    )
                    await asyncio.sleep(delay)

        # All retries failed
        self.metrics.operations_failed += 1
        self.circuit_breaker.record_failure()
        logger.warning(f"Redis operation failed after {self.config.retry_attempts} attempts: {last_error}")

        if fallback:
            self.metrics.operations_fallback += 1
            return await fallback(*args, **kwargs) if inspect.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
        return None

    def _is_connection_error(self, error: Exception) -> bool:
        """Check if error indicates connection is dead."""
        error_str = str(error).lower()
        connection_errors = [
            "connection",
            "timeout",
            "closed",
            "reset",
            "refused",
            "unavailable",
        ]
        return any(err in error_str for err in connection_errors)

    async def _restore_metrics(self) -> None:
        """Restore metrics and circuit breaker state from Redis on startup."""
        if self._redis is None:
            return
        try:
            raw_metrics = await self._redis.get(METRICS_REDIS_KEY)
            if raw_metrics:
                self.metrics.restore_from_persist(raw_metrics)

            raw_cb = await self._redis.get(CB_REDIS_KEY)
            if raw_cb:
                self.circuit_breaker.restore_from_persist(raw_cb)
        except Exception as e:
            logger.debug(f"Could not restore persisted metrics: {e}")

    async def _persist_metrics(self) -> None:
        """Flush current metrics and circuit breaker state to Redis."""
        if self._redis is None:
            return
        try:
            await self._redis.set(METRICS_REDIS_KEY, self.metrics.snapshot_for_persist())
            await self._redis.set(CB_REDIS_KEY, self.circuit_breaker.snapshot_for_persist())
        except Exception as e:
            logger.debug(f"Could not persist metrics to Redis: {e}")

    def _ensure_health_check_running(self) -> None:
        """Start health check background task if not running."""
        if self._health_check_task is None or self._health_check_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._health_check_task = loop.create_task(self._health_check_loop())
            except RuntimeError:
                pass  # No running loop

    async def _health_check_loop(self) -> None:
        """Periodic health check to detect failures proactively."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                if self._redis is not None:
                    self.metrics.health_checks_total += 1
                    self.metrics.last_health_check = time.time()

                    try:
                        await asyncio.wait_for(
                            self._redis.ping(),
                            timeout=self.config.socket_timeout
                        )
                        self.metrics.last_healthy = time.time()
                        self.circuit_breaker.record_success()
                        # Persist metrics every health check cycle
                        await self._persist_metrics()
                    except Exception as e:
                        self.metrics.health_checks_failed += 1
                        logger.warning(f"Redis health check failed: {e}")
                        self._available = False
                        self._redis = None
                        self.circuit_breaker.record_failure()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Health check loop error: {e}")

    async def close(self) -> None:
        """Close Redis connection and cleanup."""
        self._shutdown = True

        if self._health_check_task is not None:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Final metrics flush before closing
        await self._persist_metrics()

        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._redis = None

        self._available = None

    def is_available(self) -> bool:
        """
        Check if Redis is likely available (non-blocking).

        Uses cached state and circuit breaker - doesn't actually ping.
        """
        if not self.config.enabled:
            return False
        if not self.circuit_breaker.is_available():
            return False
        if self._available is not None:
            return self._available
        return True  # Optimistic before first connection

    async def health_check(self) -> Dict[str, Any]:
        """Get comprehensive health status."""
        status = {
            "enabled": self.config.enabled,
            "circuit_breaker": self.circuit_breaker.state,
            "available": self._available,
            "metrics": self.metrics.to_dict(),
        }

        if self._redis is not None:
            try:
                await asyncio.wait_for(self._redis.ping(), timeout=2.0)
                status["ping"] = "ok"
                status["connected"] = True
            except Exception as e:
                status["ping"] = f"failed: {e}"
                status["connected"] = False
        else:
            status["connected"] = False

        # Add config info
        status["config"] = {
            "url": self.config.url.replace(self.config.url.split("@")[-1].split("/")[0], "***") if "@" in self.config.url else self.config.url,
            "pool_size": self.config.pool_size,
            "retry_attempts": self.config.retry_attempts,
            "circuit_breaker_threshold": self.config.circuit_breaker_threshold,
            "sentinel_enabled": bool(self.config.sentinel_hosts),
        }

        return status

    def reset(self) -> None:
        """Reset client state (for testing)."""
        self._redis = None
        self._available = None
        self._metrics_restored = False
        self.circuit_breaker.reset()
        self.metrics = RedisMetrics()


# =============================================================================
# GLOBAL SINGLETON
# =============================================================================

_client: Optional[ResilientRedisClient] = None
_client_lock = threading.Lock()


def _get_client() -> ResilientRedisClient:
    """Get or create singleton client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = ResilientRedisClient()
    return _client


async def get_redis() -> Optional[Any]:
    """
    Get async Redis client instance.

    Returns:
        Redis client if available, None if disabled/unavailable.

    Thread-safe with circuit breaker and automatic reconnection.
    """
    return await _get_client().get()


def is_redis_available() -> bool:
    """
    Check if Redis is available (non-blocking).

    Uses cached availability status and circuit breaker state.
    """
    return _get_client().is_available()


async def close_redis() -> None:
    """Close Redis connection (call on shutdown)."""
    if _client is not None:
        await _client.close()


def reset_redis_state() -> None:
    """Reset Redis state (for testing)."""
    if _client is not None:
        _client.reset()


async def get_redis_metrics() -> Dict[str, Any]:
    """Get Redis resilience metrics."""
    return await _get_client().health_check()


def get_circuit_breaker() -> CircuitBreaker:
    """Get circuit breaker instance (for testing/monitoring)."""
    return _get_client().circuit_breaker


# =============================================================================
# CONVENIENCE DECORATORS
# =============================================================================

def with_redis_fallback(fallback_value: Any = None):
    """
    Decorator for Redis operations with automatic fallback.

    Usage:
        @with_redis_fallback(fallback_value=[])
        async def get_cached_items(redis, key):
            return await redis.lrange(key, 0, -1)
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            client = _get_client()

            async def operation(redis, *a, **kw):
                return await func(redis, *a, **kw)

            async def fallback(*a, **kw):
                return fallback_value

            return await client.execute_with_retry(operation, *args, fallback=fallback, **kwargs)
        return wrapper
    return decorator
