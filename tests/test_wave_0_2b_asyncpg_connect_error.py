"""Wave 0 step 2B — coordination_failure.asyncpg_connect_error wires.

Two sub-types per the v0.2 scoping doc §1, post-2A pivot adapter:

  - bootstrap : `_create_pool` raise paths in src/db/postgres_backend.py
  - runtime   : `_AcquireContext.__aenter__` TimeoutError path in same file

Both routes emit via `emit_coordination_failure_sync` (the 2A sync path) —
NOT via direct asyncpg-on-the-anyio-loop, which the 3-agent council BLOCKED
on PR #342's v0.2 doc. The bootstrap site has no pool to write to anyway;
the JSONL fallback in `audit_logger._write_entry` is the durable surface.

Pins:
  - Each emit fires with the documented event_type sub-namespace
  - payload carries incident_id (UUID string), error_class, db_url_hash,
    timeout_s, plus runtime-specific pool-size fields
  - emit failure (mocked to raise) does NOT mask the original ConnectionError
    — `emit_coordination_failure_sync` is failure-safe by contract; this
    test pins the contract holds at the wire-up site too.
  - Drift guards: event_type strings match the documented constants
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


BOOTSTRAP_EVENT_TYPE = "coordination_failure.asyncpg_connect_error.bootstrap"
RUNTIME_EVENT_TYPE = "coordination_failure.asyncpg_connect_error.runtime"


# ============================================================================
# Bootstrap site — _create_pool raise paths
# ============================================================================


class TestBootstrapEmit:
    """`_create_pool` raises ConnectionError when asyncpg.create_pool times out
    (asyncio.TimeoutError) or fails for any other reason. Both raise paths
    MUST emit `coordination_failure.asyncpg_connect_error.bootstrap` before
    re-raising the ConnectionError."""

    @pytest.mark.asyncio
    async def test_timeout_path_emits_bootstrap(self):
        """When ExecutorPool.create() times out (asyncio.TimeoutError), the
        bootstrap emit fires with error_class='TimeoutError' and the
        ConnectionError still reaches the caller."""
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()

        # Force the slow path by ensuring no pool exists
        backend._pool = None

        with patch(
            "src.db.postgres_backend.ExecutorPool.create",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ), patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync"
        ) as mock_emit:
            with pytest.raises(ConnectionError, match="connection timeout"):
                await backend._create_pool()

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["service"] == "governance_mcp"
        assert kwargs["event_type"] == BOOTSTRAP_EVENT_TYPE
        assert kwargs["payload"]["error_class"] == "TimeoutError"
        assert kwargs["payload"]["timeout_s"] == 5.0
        assert "db_url_hash" in kwargs["payload"]
        assert isinstance(kwargs["payload"]["db_url_hash"], str)
        assert len(kwargs["payload"]["db_url_hash"]) > 0
        # incident_id is a UUID-shaped string
        assert "incident_id" in kwargs["payload"]
        assert isinstance(kwargs["payload"]["incident_id"], str)
        # uuid4 is 36 chars with hyphens
        assert len(kwargs["payload"]["incident_id"]) == 36
        # No agent context at bootstrap time — pool creation is system-level
        assert kwargs["agent_id"] is None

    @pytest.mark.asyncio
    async def test_generic_exception_path_emits_bootstrap(self):
        """Any non-Timeout exception during pool creation also emits the
        bootstrap event — error_class reflects the original type."""
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()
        backend._pool = None

        with patch(
            "src.db.postgres_backend.ExecutorPool.create",
            new=AsyncMock(side_effect=OSError("connection refused")),
        ), patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync"
        ) as mock_emit:
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await backend._create_pool()

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["event_type"] == BOOTSTRAP_EVENT_TYPE
        assert kwargs["payload"]["error_class"] == "OSError"

    @pytest.mark.asyncio
    async def test_emit_does_not_mask_original_connection_error(self):
        """If `emit_coordination_failure_sync` itself raises (it is
        contractually failure-safe, but defense-in-depth: the wire MUST
        also tolerate a raising emit), the caller still receives the
        original ConnectionError — not the emit-failure traceback."""
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()
        backend._pool = None

        with patch(
            "src.db.postgres_backend.ExecutorPool.create",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ), patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync",
            side_effect=RuntimeError("emit blew up"),
        ):
            # The original ConnectionError must propagate, not RuntimeError
            with pytest.raises(ConnectionError, match="connection timeout"):
                await backend._create_pool()

    @pytest.mark.asyncio
    async def test_db_url_hash_does_not_leak_credentials(self):
        """The payload's db_url_hash must NOT contain the raw connection string
        (which often embeds password). Hash discipline matches the v0.2 spec."""
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()
        backend._pool = None
        backend._db_url = "postgresql://admin:supersecretpw@example.test:5432/mydb"

        with patch(
            "src.db.postgres_backend.ExecutorPool.create",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ), patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync"
        ) as mock_emit:
            with pytest.raises(ConnectionError):
                await backend._create_pool()

        payload = mock_emit.call_args.kwargs["payload"]
        hash_value = payload["db_url_hash"]
        assert "supersecretpw" not in hash_value
        assert "admin" not in hash_value
        assert "example.test" not in hash_value


# ============================================================================
# Runtime site — _AcquireContext.__aenter__ TimeoutError path
# ============================================================================


class TestRuntimeEmit:
    """When pool exists and `pool.acquire(timeout=...)` times out, the
    backend translates to a `ConnectionError("pool exhausted")`. That
    branch emits a coordination_failure event before raising. Wave 0 step
    2C-2 (post-council) splits the event_type by saturation: this test
    pins the substrate-side path (idle capacity OR pool not at max) — the
    saturation case lives in tests/test_wave_0_2c_2_executor_pool_exhaustion.py."""

    @pytest.mark.asyncio
    async def test_acquire_timeout_emits_runtime_event(self):
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_pool.release = AsyncMock()
        # Non-saturation: idle > 0 means a connection should have been
        # available — the timeout is substrate-side, not pool exhaustion.
        # Keeps this test on the asyncpg_connect_error.runtime contract.
        mock_pool.get_size = MagicMock(return_value=10)
        mock_pool.get_max_size = MagicMock(return_value=25)
        mock_pool.get_idle_size = MagicMock(return_value=3)
        backend._pool = mock_pool

        with patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync"
        ) as mock_emit:
            with pytest.raises(ConnectionError, match="pool exhausted"):
                async with backend.acquire():
                    pytest.fail("body must not execute")

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["service"] == "governance_mcp"
        assert kwargs["event_type"] == RUNTIME_EVENT_TYPE
        payload = kwargs["payload"]
        assert payload["error_class"] == "TimeoutError"
        assert payload["pool_size"] == 10
        assert payload["pool_max"] == 25
        assert payload["pool_idle"] == 3
        assert "incident_id" in payload
        assert len(payload["incident_id"]) == 36

    @pytest.mark.asyncio
    async def test_runtime_emit_does_not_mask_connection_error(self):
        """Defense-in-depth: a raising emit at the runtime wire-up site
        MUST NOT replace the user-facing ConnectionError."""
        from src.db.postgres_backend import PostgresBackend

        backend = PostgresBackend()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_pool.release = AsyncMock()
        mock_pool.get_size = MagicMock(return_value=10)
        mock_pool.get_max_size = MagicMock(return_value=10)
        mock_pool.get_idle_size = MagicMock(return_value=0)
        backend._pool = mock_pool

        with patch(
            "src.coordination_failure_emit.emit_coordination_failure_sync",
            side_effect=RuntimeError("emit blew up"),
        ):
            with pytest.raises(ConnectionError, match="pool exhausted"):
                async with backend.acquire():
                    pytest.fail("body must not execute")
