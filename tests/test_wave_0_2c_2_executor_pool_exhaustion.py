"""Wave 0 step 2C-2 — `coordination_failure.executor_pool_exhaustion.acquire_timeout`.

The 3-agent council on the v0.2 doc's polling-counter design returned BLOCK on
all three lanes (counter leak on the production `__await__` path; counter leak
on CancelledError; threshold structurally unreachable; redundant with 2B's
already-shipped acquire-timeout emit).

Reshape: drop the polling counter entirely. At the same `_AcquireContext.__aenter__`
TimeoutError site already wired in 2B, choose the event_type based on a
saturation discriminator (`pool_size == pool_max AND pool_idle == 0`):
  - saturation        → `coordination_failure.executor_pool_exhaustion.acquire_timeout`
  - non-saturation    → `coordination_failure.asyncpg_connect_error.runtime` (2B)

Carves the saturation case out of the `asyncpg_connect_error` namespace, which
is reserved for substrate-unreachable semantics (architect FINDING 10).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


SATURATION_EVENT_TYPE = "coordination_failure.executor_pool_exhaustion.acquire_timeout"
RUNTIME_EVENT_TYPE = "coordination_failure.asyncpg_connect_error.runtime"


def _make_pool(*, size: int, max_size: int, idle: int):
    pool = AsyncMock()
    pool.acquire = AsyncMock(side_effect=asyncio.TimeoutError())
    pool.release = AsyncMock()
    pool.get_size = MagicMock(return_value=size)
    pool.get_max_size = MagicMock(return_value=max_size)
    pool.get_idle_size = MagicMock(return_value=idle)
    return pool


@pytest.mark.asyncio
async def test_saturation_emits_executor_pool_exhaustion():
    """When the pool is fully saturated (size==max AND idle==0) at TimeoutError,
    the emit is `executor_pool_exhaustion.acquire_timeout` — NOT the substrate-
    unreachable `asyncpg_connect_error.runtime` family."""
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    backend._pool = _make_pool(size=25, max_size=25, idle=0)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        with pytest.raises(ConnectionError, match="pool exhausted"):
            async with backend.acquire():
                pytest.fail("body must not execute")

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["event_type"] == SATURATION_EVENT_TYPE
    payload = kwargs["payload"]
    assert payload["error_class"] == "TimeoutError"
    assert payload["pool_size"] == 25
    assert payload["pool_max"] == 25
    assert payload["pool_idle"] == 0
    assert "incident_id" in payload
    assert len(payload["incident_id"]) == 36


@pytest.mark.asyncio
async def test_non_saturation_keeps_asyncpg_connect_error_runtime():
    """If the pool has idle capacity OR isn't at max, a TimeoutError reflects
    substrate trouble (asyncpg internals couldn't deliver a connection that
    SHOULD have been available). Preserve the 2B contract — emit
    `asyncpg_connect_error.runtime`."""
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    # Idle > 0 means a connection should have been available immediately —
    # if we still timed out, it's a substrate / asyncpg-internal issue, not saturation.
    backend._pool = _make_pool(size=10, max_size=25, idle=3)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        with pytest.raises(ConnectionError, match="pool exhausted"):
            async with backend.acquire():
                pytest.fail("body must not execute")

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["event_type"] == RUNTIME_EVENT_TYPE


@pytest.mark.asyncio
async def test_size_at_max_but_some_idle_is_not_saturation():
    """Boundary: size==max but idle>0 means connections exist but are returning
    too slowly. NOT saturation — keep `asyncpg_connect_error.runtime`."""
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    backend._pool = _make_pool(size=25, max_size=25, idle=2)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        with pytest.raises(ConnectionError):
            async with backend.acquire():
                pytest.fail("body must not execute")

    assert mock_emit.call_args.kwargs["event_type"] == RUNTIME_EVENT_TYPE


@pytest.mark.asyncio
async def test_idle_zero_but_size_below_max_is_not_saturation():
    """Boundary: idle==0 but size<max means asyncpg COULD have created a new
    connection — the timeout is substrate-side, not saturation."""
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    backend._pool = _make_pool(size=15, max_size=25, idle=0)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        with pytest.raises(ConnectionError):
            async with backend.acquire():
                pytest.fail("body must not execute")

    assert mock_emit.call_args.kwargs["event_type"] == RUNTIME_EVENT_TYPE


@pytest.mark.asyncio
async def test_emit_failure_does_not_mask_connection_error():
    """Failure-safety regression: even after the discriminator change, a raising
    emit MUST NOT mask the user-facing ConnectionError."""
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    backend._pool = _make_pool(size=25, max_size=25, idle=0)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync",
        side_effect=RuntimeError("emit blew up"),
    ):
        with pytest.raises(ConnectionError, match="pool exhausted"):
            async with backend.acquire():
                pytest.fail("body must not execute")
