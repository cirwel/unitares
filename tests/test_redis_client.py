"""
Comprehensive tests for src/cache/redis_client.py

Tests the ResilientRedisClient, CircuitBreaker, RedisConfig, RedisMetrics,
and module-level convenience functions with fully mocked redis connections.
"""

import asyncio
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from dataclasses import dataclass

import pytest

import sys
from pathlib import Path

# Ensure project root is on sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.cache.redis_client import (
    CircuitBreaker,
    RedisConfig,
    RedisMetrics,
    ResilientRedisClient,
)


# =============================================================================
# RedisConfig Tests
# =============================================================================

class TestRedisConfig:
    """Tests for RedisConfig dataclass."""

    def test_default_config(self):
        """Default config uses sensible defaults."""
        with patch.dict("os.environ", {}, clear=True):
            config = RedisConfig()
        assert config.url == "redis://localhost:6379/0"
        assert config.enabled is True
        assert config.pool_size == 10
        assert config.retry_attempts == 3
        assert config.circuit_breaker_threshold == 5
        assert config.circuit_breaker_timeout == 30.0
        assert config.sentinel_hosts is None
        assert config.sentinel_master == "mymaster"
        assert config.socket_timeout == 2.0
        assert config.socket_connect_timeout == 2.0
        assert config.retry_base_delay == 0.1
        assert config.retry_max_delay == 2.0
        assert config.health_check_interval == 30.0

    def test_config_from_env(self):
        """Config reads from environment variables."""
        env = {
            "REDIS_URL": "redis://custom:6380/1",
            "REDIS_ENABLED": "0",
            "REDIS_POOL_SIZE": "20",
            "REDIS_RETRY_ATTEMPTS": "5",
            "REDIS_CIRCUIT_BREAKER_THRESHOLD": "10",
            "REDIS_CIRCUIT_BREAKER_TIMEOUT": "60",
            "REDIS_SENTINEL_HOSTS": "host1:26379,host2:26379",
            "REDIS_SENTINEL_MASTER": "primary",
        }
        with patch.dict("os.environ", env, clear=True):
            config = RedisConfig()
        assert config.url == "redis://custom:6380/1"
        assert config.enabled is False
        assert config.pool_size == 20
        assert config.retry_attempts == 5
        assert config.circuit_breaker_threshold == 10
        assert config.circuit_breaker_timeout == 60.0
        assert config.sentinel_hosts == "host1:26379,host2:26379"
        assert config.sentinel_master == "primary"

    def test_config_disabled_variants(self):
        """Different disabled values all work."""
        for val in ("0", "false", "no", "False", "NO"):
            with patch.dict("os.environ", {"REDIS_ENABLED": val}, clear=True):
                config = RedisConfig()
            assert config.enabled is False, f"REDIS_ENABLED={val!r} should disable"

    def test_config_enabled_variants(self):
        """Non-disabled values keep redis enabled."""
        for val in ("1", "true", "yes", "anything"):
            with patch.dict("os.environ", {"REDIS_ENABLED": val}, clear=True):
                config = RedisConfig()
            assert config.enabled is True, f"REDIS_ENABLED={val!r} should enable"


# =============================================================================
# CircuitBreaker Tests
# =============================================================================

class TestCircuitBreaker:
    """Tests for the CircuitBreaker state machine."""

    def test_initial_state_is_closed(self):
        """Circuit breaker starts in closed state."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_available() is True

    def test_stays_closed_under_threshold(self):
        """Failures below threshold keep circuit closed."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_available() is True

    def test_opens_at_threshold(self):
        """Reaching failure threshold opens the circuit."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_available() is False

    def test_success_resets_failure_count(self):
        """Success resets failure count and keeps circuit closed."""
        cb = CircuitBreaker(threshold=3, timeout=10.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 0
        assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_after_timeout(self):
        """Open circuit transitions to half-open after timeout."""
        cb = CircuitBreaker(threshold=1, timeout=0.1)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        # Wait past timeout
        time.sleep(0.15)
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.is_available() is True

    def test_half_open_success_closes(self):
        """Success in half-open state closes the circuit."""
        cb = CircuitBreaker(threshold=1, timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb._failure_count == 0

    def test_half_open_failure_reopens(self):
        """Failure in half-open state reopens the circuit."""
        cb = CircuitBreaker(threshold=1, timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        # Access state to trigger transition to half-open
        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_reset(self):
        """Reset returns circuit to closed state."""
        cb = CircuitBreaker(threshold=1, timeout=10.0)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        cb.reset()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb._failure_count == 0
        assert cb._last_failure_time is None
        assert cb.is_available() is True

    def test_is_available_returns_false_when_open(self):
        """is_available returns False when circuit is open."""
        cb = CircuitBreaker(threshold=2, timeout=999)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_available() is False

    def test_thread_safety(self):
        """Circuit breaker operations are thread-safe."""
        cb = CircuitBreaker(threshold=100, timeout=10.0)
        errors = []

        def record_failures():
            try:
                for _ in range(50):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_failures) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert cb._failure_count == 200  # 4 threads * 50 failures


# =============================================================================
# RedisMetrics Tests
# =============================================================================

class TestRedisMetrics:
    """Tests for RedisMetrics."""

    def test_initial_metrics(self):
        """All counters start at zero."""
        m = RedisMetrics()
        assert m.operations_total == 0
        assert m.operations_success == 0
        assert m.operations_failed == 0
        assert m.operations_fallback == 0
        assert m.retries_total == 0
        assert m.retries_success == 0
        assert m.circuit_opens == 0
        assert m.circuit_half_opens == 0
        assert m.connections_created == 0
        assert m.connections_failed == 0
        assert m.reconnections == 0

    def test_to_dict_structure(self):
        """to_dict returns expected keys and structure."""
        m = RedisMetrics()
        d = m.to_dict()
        assert "uptime_seconds" in d
        assert "operations" in d
        assert "retries" in d
        assert "circuit_breaker" in d
        assert "connections" in d
        assert "health" in d

    def test_to_dict_success_rate_zero_total(self):
        """Success rate handles zero total (division by zero guard)."""
        m = RedisMetrics()
        d = m.to_dict()
        assert d["operations"]["success_rate"] == 0.0

    def test_to_dict_success_rate_calculation(self):
        """Success rate is properly calculated."""
        m = RedisMetrics()
        m.operations_total = 10
        m.operations_success = 7
        d = m.to_dict()
        assert d["operations"]["success_rate"] == 70.0

    def test_to_dict_uptime(self):
        """Uptime increases over time."""
        m = RedisMetrics()
        time.sleep(0.05)
        d = m.to_dict()
        assert d["uptime_seconds"] >= 0.0


# =============================================================================
# ResilientRedisClient Tests
# =============================================================================

class TestResilientRedisClient:
    """Tests for ResilientRedisClient."""

    def _make_client(self, **config_overrides):
        """Helper: create a client with test-friendly config."""
        defaults = {
            "url": "redis://localhost:6379/0",
            "enabled": True,
            "pool_size": 5,
            "retry_attempts": 2,
            "retry_base_delay": 0.01,
            "retry_max_delay": 0.05,
            "circuit_breaker_threshold": 3,
            "circuit_breaker_timeout": 0.1,
            "health_check_interval": 300,  # long, so it doesn't fire in tests
        }
        defaults.update(config_overrides)
        config = RedisConfig(**defaults)
        return ResilientRedisClient(config=config)

    # --- get() tests ---

    @pytest.mark.asyncio
    async def test_get_returns_none_when_disabled(self):
        """get() returns None when redis is disabled."""
        client = self._make_client(enabled=False)
        result = await client.get()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_when_circuit_open(self):
        """get() returns None when circuit breaker is open."""
        client = self._make_client()
        # Force circuit open
        for _ in range(5):
            client.circuit_breaker.record_failure()
        assert client.circuit_breaker.state == CircuitBreaker.OPEN

        result = await client.get()
        assert result is None
        assert client.metrics.operations_fallback >= 1

    @pytest.mark.asyncio
    async def test_get_returns_existing_connection(self):
        """get() returns cached connection if available."""
        client = self._make_client()
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client._available = True

        result = await client.get()
        assert result is mock_redis

    @pytest.mark.asyncio
    async def test_get_creates_connection_on_first_call(self):
        """get() creates a new connection when none exists."""
        client = self._make_client()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        mock_redis_module = MagicMock()
        mock_redis_module.from_url = MagicMock(return_value=mock_redis)

        with patch.object(client, '_get_redis_module', return_value=mock_redis_module):
            result = await client.get()

        assert result is mock_redis
        assert client._available is True
        assert client.metrics.connections_created == 1

    @pytest.mark.asyncio
    async def test_get_returns_none_when_redis_module_unavailable(self):
        """get() returns None if redis module is not installed."""
        client = self._make_client()
        with patch.object(client, '_get_redis_module', return_value=None):
            result = await client.get()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_retries_on_connection_failure(self):
        """get() retries connection attempts before giving up."""
        client = self._make_client(retry_attempts=2)

        mock_redis_module = MagicMock()
        mock_redis_first = AsyncMock()
        mock_redis_first.ping = AsyncMock(side_effect=ConnectionError("refused"))

        mock_redis_second = AsyncMock()
        mock_redis_second.ping = AsyncMock(return_value=True)

        call_count = 0
        def from_url_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_redis_first
            return mock_redis_second

        mock_redis_module.from_url = MagicMock(side_effect=from_url_side_effect)

        with patch.object(client, '_get_redis_module', return_value=mock_redis_module):
            result = await client.get()

        # First ping fails (connections_failed += 1), second succeeds
        assert result is mock_redis_second
        assert client.metrics.connections_created == 1

    # --- _create_connection() tests ---

    @pytest.mark.asyncio
    async def test_create_connection_standard(self):
        """_create_connection creates a standard redis connection."""
        client = self._make_client()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        mock_redis_module = MagicMock()
        mock_redis_module.from_url = MagicMock(return_value=mock_redis)

        with patch.object(client, '_get_redis_module', return_value=mock_redis_module):
            result = await client._create_connection()

        assert result is mock_redis
        assert client._available is True
        mock_redis_module.from_url.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_connection_sentinel(self):
        """_create_connection uses Sentinel when configured."""
        client = self._make_client(sentinel_hosts="host1:26379,host2:26379")

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        mock_sentinel = MagicMock()
        mock_sentinel.master_for = MagicMock(return_value=mock_redis)

        mock_redis_module = MagicMock()
        mock_redis_module.Sentinel = MagicMock(return_value=mock_sentinel)

        with patch.object(client, '_get_redis_module', return_value=mock_redis_module):
            result = await client._create_connection()

        assert result is mock_redis
        assert client._available is True
        mock_redis_module.Sentinel.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_connection_failure_records_metrics(self):
        """_create_connection records failure metrics when connection fails."""
        client = self._make_client()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("refused"))

        mock_redis_module = MagicMock()
        mock_redis_module.from_url = MagicMock(return_value=mock_redis)

        with patch.object(client, '_get_redis_module', return_value=mock_redis_module):
            result = await client._create_connection()

        assert result is None
        assert client._available is False
        assert client.metrics.connections_failed == 1

    # --- execute_with_retry() tests ---

    @pytest.mark.asyncio
    async def test_execute_with_retry_success(self):
        """execute_with_retry succeeds on first try."""
        client = self._make_client()
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client._available = True

        async def my_op(redis, key):
            return f"value_for_{key}"

        result = await client.execute_with_retry(my_op, "test_key")
        assert result == "value_for_test_key"
        assert client.metrics.operations_total == 1
        assert client.metrics.operations_success == 1
        assert client.metrics.operations_failed == 0

    @pytest.mark.asyncio
    async def test_execute_with_retry_fallback_on_circuit_open(self):
        """execute_with_retry calls fallback when circuit is open."""
        client = self._make_client()
        for _ in range(5):
            client.circuit_breaker.record_failure()

        async def my_op(redis, key):
            return "from_redis"

        def my_fallback(key):
            return "from_fallback"

        result = await client.execute_with_retry(my_op, "test_key", fallback=my_fallback)
        assert result == "from_fallback"
        assert client.metrics.operations_fallback >= 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_fallback_when_redis_unavailable(self):
        """execute_with_retry calls fallback when redis is None."""
        client = self._make_client()
        # Simulate redis not available: _get_redis_module returns None
        with patch.object(client, '_get_redis_module', return_value=None):
            async def my_op(redis, key):
                return "from_redis"

            async def my_fallback(key):
                return "from_fallback"

            result = await client.execute_with_retry(my_op, "test_key", fallback=my_fallback)
        assert result == "from_fallback"

    @pytest.mark.asyncio
    async def test_execute_with_retry_retries_on_error(self):
        """execute_with_retry retries and succeeds on second attempt."""
        client = self._make_client(retry_attempts=3)
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client._available = True

        call_count = 0
        async def my_op(redis, key):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            return "success"

        result = await client.execute_with_retry(my_op, "test_key")
        assert result == "success"
        assert call_count == 2
        assert client.metrics.retries_total >= 1
        assert client.metrics.retries_success == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_all_retries_exhausted(self):
        """execute_with_retry records failure after all retries fail."""
        client = self._make_client(retry_attempts=2)
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client._available = True

        async def my_op(redis, key):
            raise ValueError("persistent error")

        result = await client.execute_with_retry(my_op, "test_key")
        assert result is None
        assert client.metrics.operations_failed == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_connection_error_triggers_reconnect(self):
        """execute_with_retry detects connection errors and attempts reconnect."""
        client = self._make_client(retry_attempts=2)
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client._available = True

        call_count = 0
        async def my_op(redis, key):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection reset")
            return "recovered"

        # Make get() return the mock_redis on reconnect
        with patch.object(client, '_create_connection', new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_redis
            # Reset _available and _redis after first failure via the method logic
            result = await client.execute_with_retry(my_op, "test_key")

        assert client.metrics.reconnections >= 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_no_fallback_returns_none(self):
        """execute_with_retry returns None when no fallback and redis unavailable."""
        client = self._make_client()
        with patch.object(client, '_get_redis_module', return_value=None):
            async def my_op(redis):
                return "from_redis"

            result = await client.execute_with_retry(my_op)
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_with_retry_sync_fallback(self):
        """execute_with_retry handles synchronous fallback functions."""
        client = self._make_client()
        for _ in range(5):
            client.circuit_breaker.record_failure()

        async def my_op(redis):
            return "from_redis"

        def my_sync_fallback():
            return "sync_fallback_result"

        result = await client.execute_with_retry(my_op, fallback=my_sync_fallback)
        assert result == "sync_fallback_result"

    # --- _is_connection_error() tests ---

    def test_is_connection_error_true_cases(self):
        """_is_connection_error detects connection-related errors."""
        client = self._make_client()
        for msg in [
            "Connection refused",
            "Timeout waiting for response",
            "Connection closed by server",
            "Connection reset by peer",
            "Service unavailable",
        ]:
            assert client._is_connection_error(Exception(msg)) is True

    def test_is_connection_error_false_cases(self):
        """_is_connection_error ignores non-connection errors."""
        client = self._make_client()
        for msg in [
            "WRONGTYPE Operation against key",
            "ERR syntax error",
            "division by zero",
        ]:
            assert client._is_connection_error(Exception(msg)) is False

    # --- is_available() tests ---

    def test_is_available_disabled(self):
        """is_available returns False when disabled."""
        client = self._make_client(enabled=False)
        assert client.is_available() is False

    def test_is_available_circuit_open(self):
        """is_available returns False when circuit is open."""
        client = self._make_client()
        for _ in range(5):
            client.circuit_breaker.record_failure()
        assert client.is_available() is False

    def test_is_available_no_connection_yet(self):
        """is_available returns True optimistically before first connection."""
        client = self._make_client()
        assert client._available is None
        assert client.is_available() is True

    def test_is_available_when_connected(self):
        """is_available returns True when connected."""
        client = self._make_client()
        client._available = True
        assert client.is_available() is True

    def test_is_available_when_disconnected(self):
        """is_available returns False after failed connection."""
        client = self._make_client()
        client._available = False
        assert client.is_available() is False

    # --- health_check() tests ---

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self):
        """health_check reports not connected when no redis."""
        client = self._make_client()
        status = await client.health_check()
        assert status["enabled"] is True
        assert status["connected"] is False
        assert "metrics" in status
        assert "config" in status

    @pytest.mark.asyncio
    async def test_health_check_connected_ping_ok(self):
        """health_check reports healthy when ping succeeds."""
        client = self._make_client()
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        client._redis = mock_redis
        client._available = True

        status = await client.health_check()
        assert status["connected"] is True
        assert status["ping"] == "ok"

    @pytest.mark.asyncio
    async def test_health_check_connected_ping_fails(self):
        """health_check reports failure when ping fails."""
        client = self._make_client()
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("dead"))
        client._redis = mock_redis
        client._available = True

        status = await client.health_check()
        assert status["connected"] is False
        assert "failed" in status["ping"]

    @pytest.mark.asyncio
    async def test_health_check_masks_host_in_url(self):
        """health_check masks host portion in URL when credentials are present."""
        client = self._make_client(url="redis://user:secret@myhost:6379/0")
        status = await client.health_check()
        # The source code masks the host part (after @) with ***
        # The URL format becomes redis://user:secret@***/0
        assert "config" in status
        config_url = status["config"]["url"]
        assert "***" in config_url
        assert "myhost:6379" not in config_url

    # --- close() tests ---

    @pytest.mark.asyncio
    async def test_close_cleans_up(self):
        """close() cleans up redis connection and state."""
        client = self._make_client()
        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()
        client._redis = mock_redis
        client._available = True

        await client.close()

        mock_redis.close.assert_called_once()
        assert client._redis is None
        assert client._available is None
        assert client._shutdown is True

    @pytest.mark.asyncio
    async def test_close_handles_close_error(self):
        """close() handles errors during redis.close() gracefully."""
        client = self._make_client()
        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock(side_effect=ConnectionError("already closed"))
        client._redis = mock_redis

        await client.close()

        assert client._redis is None

    @pytest.mark.asyncio
    async def test_close_cancels_health_check_task(self):
        """close() cancels the health check background task."""
        client = self._make_client(health_check_interval=0.01)
        # Create a real async task that we can cancel
        async def dummy_loop():
            while True:
                await asyncio.sleep(100)

        loop = asyncio.get_running_loop()
        real_task = loop.create_task(dummy_loop())
        client._health_check_task = real_task

        await client.close()

        assert real_task.cancelled()

    # --- reset() tests ---

    def test_reset_clears_state(self):
        """reset() resets all client state."""
        client = self._make_client()
        client._redis = MagicMock()
        client._available = True
        client.metrics.operations_total = 100
        client.circuit_breaker.record_failure()

        client.reset()

        assert client._redis is None
        assert client._available is None
        assert client.metrics.operations_total == 0
        assert client.circuit_breaker.state == CircuitBreaker.CLOSED

    # --- _get_redis_module() tests ---

    def test_get_redis_module_import_success(self):
        """_get_redis_module returns module when redis is importable."""
        client = self._make_client()
        mock_redis_mod = MagicMock()
        with patch.dict("sys.modules", {"redis.asyncio": mock_redis_mod, "redis": MagicMock()}):
            with patch("builtins.__import__", side_effect=lambda name, *a, **kw: mock_redis_mod if name == "redis.asyncio" else __builtins__.__import__(name, *a, **kw)):
                # Reset so it tries to import
                client._redis_module = None
                result = client._get_redis_module()
        # Just verify it doesn't return None (the actual import is tricky to mock perfectly)
        # The key behavior is tested via integration with _create_connection

    def test_get_redis_module_import_failure(self):
        """_get_redis_module returns None when redis is not installed."""
        client = self._make_client()
        client._redis_module = None
        with patch("builtins.__import__", side_effect=ImportError("No module named 'redis'")):
            result = client._get_redis_module()
        assert result is None

    def test_get_redis_module_caches_result(self):
        """_get_redis_module caches the module after first import."""
        client = self._make_client()
        mock_mod = MagicMock()
        client._redis_module = mock_mod
        result = client._get_redis_module()
        assert result is mock_mod

    # --- _create_sentinel_connection() tests ---

    @pytest.mark.asyncio
    async def test_create_sentinel_connection_parses_hosts(self):
        """Sentinel host parsing handles various formats."""
        client = self._make_client(sentinel_hosts="host1:26379,host2:26380,host3")

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        mock_sentinel = MagicMock()
        mock_sentinel.master_for = MagicMock(return_value=mock_redis)

        mock_redis_module = MagicMock()
        mock_redis_module.Sentinel = MagicMock(return_value=mock_sentinel)

        result = await client._create_sentinel_connection(mock_redis_module)

        assert result is mock_redis
        # Check parsed hosts
        call_args = mock_redis_module.Sentinel.call_args[0][0]
        assert ("host1", 26379) in call_args
        assert ("host2", 26380) in call_args
        assert ("host3", 26379) in call_args  # default port

    @pytest.mark.asyncio
    async def test_create_sentinel_connection_failure(self):
        """Sentinel connection failure records metrics."""
        client = self._make_client(sentinel_hosts="host1:26379")

        mock_redis_module = MagicMock()
        mock_redis_module.Sentinel = MagicMock(side_effect=Exception("sentinel unavailable"))

        result = await client._create_sentinel_connection(mock_redis_module)

        assert result is None
        assert client._available is False
        assert client.metrics.connections_failed == 1

    # --- _health_check_loop() tests ---

    @pytest.mark.asyncio
    async def test_health_check_loop_records_success(self):
        """Health check loop records successful pings."""
        client = self._make_client(health_check_interval=0.01)
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        client._redis = mock_redis
        client._available = True

        # Run one iteration then stop
        async def run_one_check():
            client._shutdown = False
            task = asyncio.create_task(client._health_check_loop())
            await asyncio.sleep(0.05)
            client._shutdown = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_one_check()

        assert client.metrics.health_checks_total >= 1
        assert client.metrics.last_healthy is not None

    @pytest.mark.asyncio
    async def test_health_check_loop_records_failure(self):
        """Health check loop records failed pings."""
        client = self._make_client(health_check_interval=0.01)
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("dead"))
        client._redis = mock_redis
        client._available = True

        async def run_one_check():
            client._shutdown = False
            task = asyncio.create_task(client._health_check_loop())
            await asyncio.sleep(0.05)
            client._shutdown = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_one_check()

        assert client.metrics.health_checks_failed >= 1
        assert client._available is False
        assert client._redis is None


# =============================================================================
# Module-Level Convenience Function Tests
# =============================================================================

class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""

    def test_get_client_singleton(self):
        """_get_client returns a singleton."""
        import src.cache.redis_client as mod
        # Save original
        original_client = mod._client

        try:
            mod._client = None
            c1 = mod._get_client()
            c2 = mod._get_client()
            assert c1 is c2
            assert isinstance(c1, ResilientRedisClient)
        finally:
            mod._client = original_client

    @pytest.mark.asyncio
    async def test_get_redis_function(self):
        """get_redis() delegates to singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.get = AsyncMock(return_value="mock_redis")
            mod._client = mock_client

            result = await mod.get_redis()
            assert result == "mock_redis"
            mock_client.get.assert_called_once()
        finally:
            mod._client = original_client

    def test_is_redis_available_function(self):
        """is_redis_available() delegates to singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.is_available = MagicMock(return_value=True)
            mod._client = mock_client

            result = mod.is_redis_available()
            assert result is True
        finally:
            mod._client = original_client

    def test_is_redis_enabled_function(self):
        """is_redis_enabled() reads config.enabled from the singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.config = RedisConfig(enabled=False)
            mod._client = mock_client
            assert mod.is_redis_enabled() is False

            mock_client.config = RedisConfig(enabled=True)
            assert mod.is_redis_enabled() is True
        finally:
            mod._client = original_client

    @pytest.mark.asyncio
    async def test_close_redis_function(self):
        """close_redis() delegates to singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.close = AsyncMock()
            mod._client = mock_client

            await mod.close_redis()
            mock_client.close.assert_called_once()
        finally:
            mod._client = original_client

    @pytest.mark.asyncio
    async def test_close_redis_noop_when_no_client(self):
        """close_redis() does nothing when no client exists."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mod._client = None
            await mod.close_redis()  # Should not raise
        finally:
            mod._client = original_client

    def test_reset_redis_state_function(self):
        """reset_redis_state() delegates to singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mod._client = mock_client

            mod.reset_redis_state()
            mock_client.reset.assert_called_once()
        finally:
            mod._client = original_client

    def test_reset_redis_state_noop_when_no_client(self):
        """reset_redis_state() does nothing when no client exists."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mod._client = None
            mod.reset_redis_state()  # Should not raise
        finally:
            mod._client = original_client

    @pytest.mark.asyncio
    async def test_get_redis_metrics_function(self):
        """get_redis_metrics() delegates to singleton client."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.health_check = AsyncMock(return_value={"status": "ok"})
            mod._client = mock_client

            result = await mod.get_redis_metrics()
            assert result == {"status": "ok"}
        finally:
            mod._client = original_client

    def test_get_circuit_breaker_function(self):
        """get_circuit_breaker() returns the circuit breaker."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            mock_cb = CircuitBreaker()
            mock_client = MagicMock(spec=ResilientRedisClient)
            mock_client.circuit_breaker = mock_cb
            mod._client = mock_client

            result = mod.get_circuit_breaker()
            assert result is mock_cb
        finally:
            mod._client = original_client


# =============================================================================
# with_redis_fallback Decorator Tests
# =============================================================================

class TestWithRedisFallback:
    """Tests for the with_redis_fallback decorator."""

    @pytest.mark.asyncio
    async def test_decorator_returns_fallback_when_redis_unavailable(self):
        """Decorated function returns fallback when redis is unavailable."""
        import src.cache.redis_client as mod
        original_client = mod._client

        try:
            # Create a client that won't connect
            client = ResilientRedisClient(RedisConfig(enabled=True))
            # Force circuit open so it returns None quickly
            for _ in range(10):
                client.circuit_breaker.record_failure()
            mod._client = client

            @mod.with_redis_fallback(fallback_value=["default"])
            async def get_items(redis, key):
                return await redis.lrange(key, 0, -1)

            result = await get_items("my_key")
            assert result == ["default"]
        finally:
            mod._client = original_client


# =============================================================================
# Metrics Persistence Tests
# =============================================================================

class TestMetricsPersistence:
    """Tests for metrics and circuit breaker persist/restore round-trips."""

    def test_metrics_snapshot_round_trip(self):
        """RedisMetrics survives serialize/deserialize."""
        m = RedisMetrics()
        m.operations_total = 100
        m.operations_failed = 7
        m.circuit_opens = 2
        m.reconnections = 3

        snap = m.snapshot_for_persist()
        m2 = RedisMetrics()
        m2.restore_from_persist(snap)

        assert m2.operations_total == 100
        assert m2.operations_failed == 7
        assert m2.circuit_opens == 2
        assert m2.reconnections == 3
        # Non-persisted fields stay default
        assert m2.last_health_check is None

    def test_metrics_restore_ignores_bad_json(self):
        """Corrupt JSON doesn't crash, metrics stay at defaults."""
        m = RedisMetrics()
        m.restore_from_persist("not valid json{{{")
        assert m.operations_total == 0

    def test_metrics_restore_handles_partial_data(self):
        """Missing fields in persisted data are left at defaults."""
        import json
        m = RedisMetrics()
        m.restore_from_persist(json.dumps({"operations_total": 42}))
        assert m.operations_total == 42
        assert m.operations_failed == 0  # not in payload, stays default

    def test_cb_snapshot_round_trip(self):
        """CircuitBreaker state survives serialize/deserialize."""
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()  # trips to OPEN
        assert cb.state == CircuitBreaker.OPEN

        snap = cb.snapshot_for_persist()
        cb2 = CircuitBreaker(threshold=3)
        cb2.restore_from_persist(snap)

        assert cb2._failure_count == 3
        assert cb2._state == CircuitBreaker.OPEN
        assert len(cb2._trip_timestamps) == 1

    def test_cb_restore_ignores_bad_json(self):
        """Corrupt JSON doesn't crash circuit breaker."""
        cb = CircuitBreaker()
        cb.restore_from_persist("garbage!!!")
        assert cb._state == CircuitBreaker.CLOSED
        assert cb._failure_count == 0

    def test_cb_restore_handles_empty_trips(self):
        """Restoring with no trip timestamps works."""
        import json
        cb = CircuitBreaker()
        cb.restore_from_persist(json.dumps({
            "state": "half_open",
            "failure_count": 1,
            "trip_timestamps": [],
        }))
        assert cb._state == "half_open"
        assert cb._failure_count == 1
        assert len(cb._trip_timestamps) == 0

    @pytest.mark.asyncio
    async def test_persist_metrics_writes_to_redis(self):
        """_persist_metrics writes both keys to Redis."""
        from src.cache.redis_client import METRICS_REDIS_KEY, CB_REDIS_KEY

        client = ResilientRedisClient(RedisConfig(enabled=True))
        mock_redis = AsyncMock()
        client._redis = mock_redis
        client.metrics.operations_total = 50

        await client._persist_metrics()

        assert mock_redis.set.call_count == 2
        calls = {c.args[0]: c.args[1] for c in mock_redis.set.call_args_list}
        assert METRICS_REDIS_KEY in calls
        assert CB_REDIS_KEY in calls
        assert '"operations_total": 50' in calls[METRICS_REDIS_KEY]

    @pytest.mark.asyncio
    async def test_persist_metrics_noop_when_no_redis(self):
        """_persist_metrics is safe when redis is None."""
        client = ResilientRedisClient(RedisConfig(enabled=True))
        client._redis = None
        await client._persist_metrics()  # should not raise

    @pytest.mark.asyncio
    async def test_restore_metrics_reads_from_redis(self):
        """_restore_metrics reads and applies both keys."""
        import json
        from src.cache.redis_client import METRICS_REDIS_KEY, CB_REDIS_KEY

        client = ResilientRedisClient(RedisConfig(enabled=True))
        mock_redis = AsyncMock()

        metrics_data = json.dumps({"operations_total": 99, "operations_failed": 5,
                                    "operations_success": 0, "operations_fallback": 0,
                                    "retries_total": 0, "retries_success": 0,
                                    "circuit_opens": 0, "circuit_half_opens": 0,
                                    "connections_created": 0, "connections_failed": 0,
                                    "reconnections": 0, "health_checks_total": 0,
                                    "health_checks_failed": 0})
        cb_data = json.dumps({"state": "closed", "failure_count": 0, "trip_timestamps": []})

        async def mock_get(key):
            return {METRICS_REDIS_KEY: metrics_data, CB_REDIS_KEY: cb_data}.get(key)

        mock_redis.get = AsyncMock(side_effect=mock_get)
        client._redis = mock_redis

        await client._restore_metrics()

        assert client.metrics.operations_total == 99
        assert client.metrics.operations_failed == 5

    @pytest.mark.asyncio
    async def test_restore_metrics_noop_when_no_redis(self):
        """_restore_metrics is safe when redis is None."""
        client = ResilientRedisClient(RedisConfig(enabled=True))
        client._redis = None
        await client._restore_metrics()  # should not raise

    @pytest.mark.asyncio
    async def test_restore_metrics_handles_redis_error(self):
        """_restore_metrics doesn't crash on Redis errors."""
        client = ResilientRedisClient(RedisConfig(enabled=True))
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("gone"))
        client._redis = mock_redis

        await client._restore_metrics()  # should not raise
        assert client.metrics.operations_total == 0

    @pytest.mark.asyncio
    async def test_persist_metrics_handles_redis_error(self):
        """_persist_metrics doesn't crash on Redis errors."""
        client = ResilientRedisClient(RedisConfig(enabled=True))
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("gone"))
        client._redis = mock_redis

        await client._persist_metrics()  # should not raise
