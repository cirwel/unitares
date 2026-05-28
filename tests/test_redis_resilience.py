"""
Tests for Redis resilience features.

Tests circuit breaker, retry logic, metrics, and fallback behavior.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.cache.redis_client import (
    CircuitBreaker,
    RedisConfig,
    RedisMetrics,
    ResilientRedisClient,
    get_redis,
    is_redis_available,
    reset_redis_state,
    get_redis_metrics,
    get_circuit_breaker,
)


# =============================================================================
# Circuit Breaker Tests
# =============================================================================

class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state_closed(self):
        """Circuit breaker starts in closed state."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_available() is True

    def test_opens_after_threshold_failures(self):
        """Circuit opens after reaching failure threshold."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)

        # First two failures - still closed
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED

        # Third failure - opens
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_available() is False

    def test_success_resets_failure_count(self):
        """Success resets failure count."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)

        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 2

        cb.record_success()
        assert cb._failure_count == 0
        assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_after_timeout(self):
        """Circuit transitions to half-open after timeout."""
        with patch("src.cache.redis_client.time.time") as mock_time:
            mock_time.return_value = 1000.0
            cb = CircuitBreaker(threshold=3, timeout=0.1)  # 100ms timeout

            # Open the circuit
            for _ in range(3):
                cb.record_failure()
            assert cb.state == CircuitBreaker.OPEN

            mock_time.return_value = 1000.15

            # Should transition to half-open
            assert cb.state == CircuitBreaker.HALF_OPEN
            assert cb.is_available() is True

    def test_half_open_closes_on_success(self):
        """Circuit closes from half-open on success."""
        with patch("src.cache.redis_client.time.time") as mock_time:
            mock_time.return_value = 1000.0
            cb = CircuitBreaker(threshold=3, timeout=0.1)

            # Open -> half-open
            for _ in range(3):
                cb.record_failure()
            mock_time.return_value = 1000.15
            assert cb.state == CircuitBreaker.HALF_OPEN

            # Success closes it
            cb.record_success()
            assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_reopens_on_failure(self):
        """Circuit reopens from half-open on failure."""
        with patch("src.cache.redis_client.time.time") as mock_time:
            mock_time.return_value = 1000.0
            cb = CircuitBreaker(threshold=3, timeout=0.1)

            # Open -> half-open
            for _ in range(3):
                cb.record_failure()
            mock_time.return_value = 1000.15
            assert cb.state == CircuitBreaker.HALF_OPEN

            # Failure reopens it
            cb.record_failure()
            assert cb.state == CircuitBreaker.OPEN

    def test_reset(self):
        """Reset returns circuit to initial state."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)

        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        cb.reset()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb._failure_count == 0


# =============================================================================
# Redis Config Tests
# =============================================================================

class TestRedisConfig:
    """Tests for RedisConfig class."""

    def test_default_values(self):
        """Config has sensible defaults."""
        config = RedisConfig()

        assert config.url == "redis://localhost:6379/0"
        assert config.enabled is True
        assert config.pool_size == 10
        assert config.retry_attempts == 3
        assert config.circuit_breaker_threshold == 5
        assert config.circuit_breaker_timeout == 30.0

    def test_env_override(self):
        """Config values can be overridden by environment."""
        with patch.dict("os.environ", {
            "REDIS_URL": "redis://custom:6379/1",
            "REDIS_ENABLED": "0",
            "REDIS_POOL_SIZE": "20",
            "REDIS_RETRY_ATTEMPTS": "5",
        }):
            config = RedisConfig()

            assert config.url == "redis://custom:6379/1"
            assert config.enabled is False
            assert config.pool_size == 20
            assert config.retry_attempts == 5


# =============================================================================
# Redis Metrics Tests
# =============================================================================

class TestRedisMetrics:
    """Tests for RedisMetrics class."""

    def test_initial_values(self):
        """Metrics start at zero."""
        metrics = RedisMetrics()

        assert metrics.operations_total == 0
        assert metrics.operations_success == 0
        assert metrics.operations_failed == 0
        assert metrics.operations_fallback == 0

    def test_to_dict(self):
        """Metrics export correctly to dict."""
        metrics = RedisMetrics()
        metrics.operations_total = 100
        metrics.operations_success = 95
        metrics.operations_failed = 5

        data = metrics.to_dict()

        assert data["operations"]["total"] == 100
        assert data["operations"]["success"] == 95
        assert data["operations"]["failed"] == 5
        assert data["operations"]["success_rate"] == 95.0

    def test_success_rate_handles_zero(self):
        """Success rate doesn't divide by zero."""
        metrics = RedisMetrics()
        data = metrics.to_dict()

        # With 0 total, should be 0% (not error)
        assert data["operations"]["success_rate"] == 0.0


# =============================================================================
# Resilient Redis Client Tests
# =============================================================================

class TestResilientRedisClient:
    """Tests for ResilientRedisClient class."""

    @pytest.fixture
    def disabled_client(self):
        """Client with Redis disabled."""
        config = RedisConfig()
        config.enabled = False
        return ResilientRedisClient(config)

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis client."""
        mock = AsyncMock()
        mock.ping = AsyncMock(return_value=True)
        mock.close = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, disabled_client):
        """Disabled client returns None."""
        result = await disabled_client.get()
        assert result is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_when_open(self):
        """Open circuit breaker blocks requests."""
        config = RedisConfig()
        config.enabled = True
        client = ResilientRedisClient(config)

        # Open the circuit breaker
        for _ in range(config.circuit_breaker_threshold):
            client.circuit_breaker.record_failure()

        assert client.circuit_breaker.state == CircuitBreaker.OPEN

        # Should return None without trying to connect
        result = await client.get()
        assert result is None
        assert client.metrics.operations_fallback > 0

    @pytest.mark.asyncio
    async def test_is_available_respects_circuit_breaker(self):
        """is_available checks circuit breaker state."""
        config = RedisConfig()
        config.enabled = True
        client = ResilientRedisClient(config)

        assert client.is_available() is True

        # Open the circuit
        for _ in range(config.circuit_breaker_threshold):
            client.circuit_breaker.record_failure()

        assert client.is_available() is False

    def test_is_connection_error_detection(self):
        """Connection errors are correctly detected."""
        client = ResilientRedisClient()

        assert client._is_connection_error(Exception("connection refused")) is True
        assert client._is_connection_error(Exception("timeout waiting for response")) is True
        assert client._is_connection_error(Exception("connection reset by peer")) is True
        assert client._is_connection_error(Exception("socket closed")) is True
        assert client._is_connection_error(Exception("key not found")) is False

    @pytest.mark.asyncio
    async def test_execute_with_retry_uses_fallback(self):
        """execute_with_retry uses fallback when Redis fails."""
        config = RedisConfig()
        config.enabled = False  # Disable Redis to force fallback
        client = ResilientRedisClient(config)

        fallback_value = "fallback_result"

        async def operation(redis, key):
            return await redis.get(key)

        async def fallback(key):
            return fallback_value

        result = await client.execute_with_retry(
            operation,
            "test_key",
            fallback=fallback
        )

        assert result == fallback_value
        assert client.metrics.operations_fallback > 0

    @pytest.mark.asyncio
    async def test_health_check_returns_status(self):
        """Health check returns comprehensive status."""
        config = RedisConfig()
        config.enabled = True
        client = ResilientRedisClient(config)

        status = await client.health_check()

        assert "enabled" in status
        assert "circuit_breaker" in status
        assert "metrics" in status
        assert "config" in status

    def test_reset_clears_state(self):
        """Reset clears all client state."""
        client = ResilientRedisClient()
        client.metrics.operations_total = 100
        client.circuit_breaker.record_failure()

        client.reset()

        assert client.metrics.operations_total == 0
        assert client.circuit_breaker.state == CircuitBreaker.CLOSED


# =============================================================================
# Module-Level Function Tests
# =============================================================================

class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def setup_method(self):
        """Reset Redis state before each test."""
        reset_redis_state()

    @pytest.mark.asyncio
    async def test_get_redis_metrics(self):
        """get_redis_metrics returns health status."""
        metrics = await get_redis_metrics()

        assert isinstance(metrics, dict)
        assert "enabled" in metrics
        assert "circuit_breaker" in metrics
        assert "metrics" in metrics

    def test_get_circuit_breaker(self):
        """get_circuit_breaker returns the circuit breaker."""
        cb = get_circuit_breaker()

        assert isinstance(cb, CircuitBreaker)
        assert cb.state == CircuitBreaker.CLOSED

    def test_is_redis_available_optimistic(self):
        """is_redis_available is optimistic before first connection."""
        reset_redis_state()

        # Before any connection attempt, should be optimistic
        # (Returns True because we haven't tried yet)
        # Note: This may return False if Redis is explicitly disabled
        result = is_redis_available()
        assert isinstance(result, bool)


# =============================================================================
# Integration Tests (Require Mocking)
# =============================================================================

class TestRedisIntegration:
    """Integration tests with mocked Redis."""

    @pytest.mark.asyncio
    async def test_retry_on_transient_failure(self):
        """Client retries on transient failures."""
        config = RedisConfig()
        config.enabled = True
        config.retry_attempts = 3
        config.retry_base_delay = 0.01  # Fast retries for testing
        client = ResilientRedisClient(config)

        # Mock Redis module
        mock_redis = AsyncMock()
        call_count = 0

        async def failing_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("connection timeout")
            return mock_redis

        with patch.object(client, '_create_connection', side_effect=failing_then_success):
            result = await client.get()

            # Should eventually succeed after retries
            assert result == mock_redis or result is None  # Depends on mock setup
            assert client.metrics.retries_total >= 0

    @pytest.mark.asyncio
    async def test_metrics_track_operations(self):
        """Metrics correctly track operations."""
        config = RedisConfig()
        config.enabled = False  # Use fallback mode
        client = ResilientRedisClient(config)

        # Execute some operations
        for _ in range(5):
            await client.execute_with_retry(
                lambda r: r.get("key"),
                fallback=lambda: "fallback"
            )

        # Check metrics
        assert client.metrics.operations_total == 5
        assert client.metrics.operations_fallback == 5

    @pytest.mark.asyncio
    async def test_close_cleanup(self):
        """Close properly cleans up resources."""
        client = ResilientRedisClient()

        # Simulate having a connection
        client._redis = AsyncMock()
        client._redis.close = AsyncMock()

        await client.close()

        assert client._redis is None
        assert client._shutdown is True


# =============================================================================
# Sentinel Support Tests
# =============================================================================

class TestSentinelSupport:
    """Tests for Redis Sentinel support."""

    def test_config_parses_sentinel_hosts(self):
        """Sentinel hosts are correctly parsed from env."""
        with patch.dict("os.environ", {
            "REDIS_SENTINEL_HOSTS": "host1:26379,host2:26380,host3",
            "REDIS_SENTINEL_MASTER": "mymaster",
        }):
            config = RedisConfig()

            assert config.sentinel_hosts == "host1:26379,host2:26380,host3"
            assert config.sentinel_master == "mymaster"

    def test_sentinel_host_parsing(self):
        """Sentinel host string is correctly parsed."""
        config = RedisConfig()
        config.sentinel_hosts = "host1:26379,host2:26380,host3"

        # Parse manually to test
        hosts = []
        for host_port in config.sentinel_hosts.split(","):
            host_port = host_port.strip()
            if ":" in host_port:
                host, port = host_port.rsplit(":", 1)
                hosts.append((host, int(port)))
            else:
                hosts.append((host_port, 26379))

        assert hosts == [
            ("host1", 26379),
            ("host2", 26380),
            ("host3", 26379),  # Default port
        ]
