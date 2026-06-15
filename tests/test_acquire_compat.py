"""Regression tests for src/db/acquire_compat.py.

compatible_acquire is a small but load-bearing shim: it encodes a *fix for a
real connection-leak bug*. The previous implementation used
inspect.isawaitable() to detect test mocks, but asyncpg's PoolAcquireContext is
ALSO awaitable, so production connections were acquired via `await` and never
released. The current code prefers the async-context-manager form whenever
__aenter__ exists, falling back to the awaitable form only for mocks.

These tests pin all three branches so that regression cannot silently return.
"""

from __future__ import annotations

import pytest

from src.db.acquire_compat import compatible_acquire


class _CtxConn:
    """A connection exposed via async-context-manager (the asyncpg shape)."""

    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return "real-conn"

    async def __aexit__(self, *exc):
        self.exited = True
        return False


class _CtxPool:
    def __init__(self, ctx):
        self._ctx = ctx

    def acquire(self):
        # asyncpg's PoolAcquireContext is both awaitable AND an async CM;
        # the shim must take the CM path to guarantee release.
        return self._ctx


class _AwaitableConn:
    """A mock connection: awaitable that resolves to itself, with async close."""

    def __init__(self):
        self.closed = False

    def __await__(self):
        async def _resolve():
            return self
        return _resolve().__await__()

    async def close(self):
        self.closed = True


class _AwaitablePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self._conn


class _BadPool:
    def acquire(self):
        return object()  # neither a context manager nor awaitable


@pytest.mark.asyncio
async def test_context_manager_path_enters_and_exits():
    ctx = _CtxConn()
    pool = _CtxPool(ctx)
    async with compatible_acquire(pool) as conn:
        assert conn == "real-conn"
        assert ctx.entered is True
        assert ctx.exited is False   # still inside
    assert ctx.exited is True        # released on exit (no leak)


@pytest.mark.asyncio
async def test_awaitable_mock_path_yields_and_closes():
    conn = _AwaitableConn()
    pool = _AwaitablePool(conn)
    async with compatible_acquire(pool) as c:
        assert c is conn
        assert conn.closed is False
    assert conn.closed is True       # best-effort release for mocks


@pytest.mark.asyncio
async def test_unexpected_acquire_type_raises():
    with pytest.raises(TypeError):
        async with compatible_acquire(_BadPool()):
            pass
