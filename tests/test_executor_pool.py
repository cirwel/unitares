"""
Tests for src/db/executor_pool.py — wraps asyncpg.Pool so all DB operations
run on a dedicated background thread with its own asyncio event loop, isolating
asyncpg from the MCP SDK's anyio task group context.

Background: (P2 design + council).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.executor_pool import ExecutorPool


@pytest.mark.asyncio
async def test_fetchval_runs_on_dedicated_thread():
    """
    The whole point of P2: the asyncpg call must execute on a different
    thread than the caller's loop. If the wrapper runs the asyncpg coroutine
    on the caller's loop, we haven't isolated anything and the deadlock stays.
    """
    caller_thread_id = threading.get_ident()
    asyncpg_thread_id_holder = {}

    async def capture_thread_and_return(*args, **kwargs):
        asyncpg_thread_id_holder['tid'] = threading.get_ident()
        return 42

    mock_conn = MagicMock()
    mock_conn.fetchval = capture_thread_and_return

    mock_acquire_ctx = AsyncMock()
    mock_acquire_ctx.__aenter__.return_value = mock_conn
    mock_acquire_ctx.__aexit__.return_value = None

    raw_pool = MagicMock()
    raw_pool.acquire = MagicMock(return_value=mock_acquire_ctx)
    raw_pool.close = AsyncMock()

    wrapped = ExecutorPool(raw_pool)
    try:
        async with wrapped.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
    finally:
        await wrapped.close()

    assert result == 42
    assert 'tid' in asyncpg_thread_id_holder, "asyncpg call never ran"
    assert asyncpg_thread_id_holder['tid'] != caller_thread_id, (
        f"asyncpg ran on caller thread {caller_thread_id} — wrapper failed "
        f"to isolate. Both got {asyncpg_thread_id_holder['tid']}."
    )


def _make_pool_with_conn(conn_methods: dict):
    """Helper: build an ExecutorPool over a mock conn whose methods we define."""
    mock_conn = MagicMock()
    for name, impl in conn_methods.items():
        setattr(mock_conn, name, impl)

    mock_acquire_ctx = AsyncMock()
    mock_acquire_ctx.__aenter__.return_value = mock_conn
    mock_acquire_ctx.__aexit__.return_value = None

    raw_pool = MagicMock()
    raw_pool.acquire = MagicMock(return_value=mock_acquire_ctx)
    raw_pool.close = AsyncMock()  # asyncpg.Pool.close is async
    return ExecutorPool(raw_pool), mock_conn


@pytest.mark.asyncio
async def test_all_connection_methods_forward():
    """fetch, fetchrow, execute, executemany all route through the executor loop."""

    async def fetch_impl(*a, **k): return [{"row": 1}]
    async def fetchrow_impl(*a, **k): return {"row": 2}
    async def execute_impl(*a, **k): return "INSERT 0 1"
    async def executemany_impl(*a, **k): return None

    wrapped, _ = _make_pool_with_conn({
        "fetch": fetch_impl,
        "fetchrow": fetchrow_impl,
        "execute": execute_impl,
        "executemany": executemany_impl,
    })
    try:
        async with wrapped.acquire() as conn:
            assert await conn.fetch("SELECT *") == [{"row": 1}]
            assert await conn.fetchrow("SELECT 1") == {"row": 2}
            assert await conn.execute("INSERT INTO x VALUES (1)") == "INSERT 0 1"
            assert await conn.executemany("INSERT INTO x VALUES ($1)", [(1,), (2,)]) is None
    finally:
        await wrapped.close()


@pytest.mark.asyncio
async def test_transaction_context_manager_round_trips():
    """
    transaction() returns an async context manager whose __aenter__/__aexit__
    must both round-trip to the executor loop. Council architect flagged this
    as easy-to-miss because it looks like pure caller-side syntax.
    """
    caller_tid = threading.get_ident()
    enter_tid = []
    exit_tid = []

    mock_txn = MagicMock()
    async def txn_aenter():
        enter_tid.append(threading.get_ident())
        return mock_txn
    async def txn_aexit(exc_type, exc_val, exc_tb):
        exit_tid.append(threading.get_ident())
        return None
    mock_txn.__aenter__ = MagicMock(side_effect=txn_aenter)
    mock_txn.__aexit__ = MagicMock(side_effect=txn_aexit)

    def transaction_impl(): return mock_txn

    wrapped, _ = _make_pool_with_conn({"transaction": transaction_impl})
    try:
        async with wrapped.acquire() as conn:
            async with conn.transaction():
                pass
    finally:
        await wrapped.close()

    assert len(enter_tid) == 1 and enter_tid[0] != caller_tid, "txn __aenter__ ran on caller thread"
    assert len(exit_tid) == 1 and exit_tid[0] != caller_tid, "txn __aexit__ ran on caller thread"


@pytest.mark.asyncio
async def test_transaction_start_commit_rollback_methods_round_trip():
    """PostgresBackend.transaction() calls start/commit/rollback directly."""
    caller_tid = threading.get_ident()
    start_tid = []
    commit_tid = []
    rollback_tid = []

    mock_txn = MagicMock()

    async def start_impl():
        start_tid.append(threading.get_ident())

    async def commit_impl():
        commit_tid.append(threading.get_ident())

    async def rollback_impl():
        rollback_tid.append(threading.get_ident())

    mock_txn.start = MagicMock(side_effect=start_impl)
    mock_txn.commit = MagicMock(side_effect=commit_impl)
    mock_txn.rollback = MagicMock(side_effect=rollback_impl)

    def transaction_impl():
        return mock_txn

    wrapped, _ = _make_pool_with_conn({"transaction": transaction_impl})
    try:
        async with wrapped.acquire() as conn:
            txn = conn.transaction()
            await txn.start()
            await txn.commit()

            txn = conn.transaction()
            await txn.start()
            await txn.rollback()
    finally:
        await wrapped.close()

    assert start_tid and all(tid != caller_tid for tid in start_tid)
    assert commit_tid == [commit_tid[0]] and commit_tid[0] != caller_tid
    assert rollback_tid == [rollback_tid[0]] and rollback_tid[0] != caller_tid


@pytest.mark.asyncio
async def test_handler_cancellation_propagates_to_executor_task():
    """
    LOAD-BEARING: when the caller's await is cancelled, the asyncpg-side
    coroutine on the executor loop must also be cancelled — not left orphaned
    holding the connection.

    Architect flagged: without this bridge, you get orphan queries that
    block subsequent acquire() calls forever.

    Note: `asyncio.wrap_future(run_coroutine_threadsafe(...))` provides this
    bridge automatically via stdlib's `_chain_future`. This test guards
    against a future refactor that uses a non-chaining bridge.
    """
    cancelled_on_executor = asyncio.Event()
    started_on_executor = threading.Event()

    async def long_query(*args, **kwargs):
        started_on_executor.set()
        try:
            await asyncio.sleep(10)  # would-be slow query
        except asyncio.CancelledError:
            cancelled_on_executor.set()
            raise
        return "should never reach"

    wrapped, _ = _make_pool_with_conn({"fetchval": long_query})
    try:
        async with wrapped.acquire() as conn:
            task = asyncio.create_task(conn.fetchval("SELECT pg_sleep(10)"))
            # Wait until the executor-side coroutine is actually running.
            await asyncio.get_event_loop().run_in_executor(
                None, started_on_executor.wait, 2.0
            )
            assert started_on_executor.is_set(), "executor task never started"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Give the bridge a moment to propagate the cancel to the executor loop.
            await asyncio.sleep(0.1)
            assert cancelled_on_executor.is_set(), (
                "Caller cancelled but executor task was not cancelled — "
                "orphan query holding the connection."
            )
    finally:
        await wrapped.close()


@pytest.mark.asyncio
async def test_close_calls_raw_pool_close_on_executor_loop():
    """
    Pool teardown must invoke raw asyncpg pool.close() on the executor loop
    (asyncpg connections are loop-bound; closing from any other loop fails).
    Architect flagged: wrong teardown order = hung shutdown or
    'pool closed mid-query' tracebacks during launchd unload.
    """
    caller_tid = threading.get_ident()
    close_tid = []

    async def close_impl():
        close_tid.append(threading.get_ident())

    raw_pool = MagicMock()
    raw_pool.close = close_impl

    wrapped = ExecutorPool(raw_pool)
    await wrapped.close()

    assert len(close_tid) == 1, "raw pool.close() was never called"
    assert close_tid[0] != caller_tid, (
        f"raw pool.close() ran on caller thread {caller_tid} — must run on "
        f"executor loop because asyncpg connections are loop-bound."
    )


@pytest.mark.asyncio
async def test_pool_pass_through_attributes():
    """
    `_closed`, `get_size`, `get_idle_size`, `get_max_size` are read by callers
    (postgres_backend.py:104, dialectic_db.py:55) — wrapper must mirror them
    so the abstraction doesn't leak.
    """
    raw_pool = MagicMock()
    raw_pool._closed = False
    raw_pool.get_size = MagicMock(return_value=10)
    raw_pool.get_idle_size = MagicMock(return_value=7)
    raw_pool.get_max_size = MagicMock(return_value=20)
    raw_pool.close = AsyncMock()

    wrapped = ExecutorPool(raw_pool)
    try:
        assert wrapped._closed is False
        assert wrapped.get_size() == 10
        assert wrapped.get_idle_size() == 7
        assert wrapped.get_max_size() == 20
    finally:
        await wrapped.close()


@pytest.mark.asyncio
async def test_await_acquire_then_release_pattern():
    """
    asyncpg's PoolAcquireContext is BOTH awaitable AND an async context manager.
    `PostgresBackend.acquire()` (postgres_backend.py:189) uses the await
    form: `conn = await pool.acquire(timeout=N)` then `await pool.release(conn)`.
    The wrapper must support this pattern or PostgresBackend.acquire() breaks.
    """
    caller_tid = threading.get_ident()
    release_tid = []

    async def fetchval_impl(*a, **k): return 7

    mock_conn = MagicMock()
    mock_conn.fetchval = fetchval_impl

    raw_pool = MagicMock()

    async def mock_acquire(*args, timeout=None):
        # Mimic asyncpg: `await pool.acquire()` returns a Connection directly.
        return mock_conn

    async def mock_release(conn):
        release_tid.append(threading.get_ident())

    raw_pool.acquire = mock_acquire
    raw_pool.release = mock_release
    raw_pool.close = AsyncMock()

    wrapped = ExecutorPool(raw_pool)
    try:
        conn = await wrapped.acquire(timeout=5)
        assert await conn.fetchval("SELECT 1") == 7
        await wrapped.release(conn)
    finally:
        await wrapped.close()

    assert release_tid == [release_tid[0]] and release_tid[0] != caller_tid, (
        "release ran on caller thread — must run on executor loop"
    )


@pytest.mark.asyncio
async def test_acquire_timeout_passes_through_in_context_form():
    """`async with pool.acquire(timeout=5)` (used at postgres_backend.py:99
    in the health check). Timeout must reach the raw asyncpg pool."""
    captured = {}

    async def fake_aenter():
        return MagicMock()
    async def fake_aexit(*a):
        return None

    def make_acquire_ctx(*args, timeout=None):
        captured['timeout'] = timeout
        ctx = MagicMock()
        ctx.__aenter__ = MagicMock(side_effect=fake_aenter)
        ctx.__aexit__ = MagicMock(side_effect=fake_aexit)
        return ctx

    raw_pool = MagicMock()
    raw_pool.acquire = make_acquire_ctx
    raw_pool.close = AsyncMock()

    wrapped = ExecutorPool(raw_pool)
    try:
        async with wrapped.acquire(timeout=5):
            pass
    finally:
        await wrapped.close()

    assert captured.get('timeout') == 5, f"timeout not forwarded: {captured}"


# ============================================================================
# Wedged-loop recovery (PR #226 followup #1)
# ============================================================================

@pytest.mark.asyncio
async def test_close_is_idempotent():
    """Second close() must be a no-op. Recovery path can issue concurrent
    close() calls when multiple failed-health-check tasks race; without
    idempotency the second one re-stops the (already-stopped) loop and
    re-issues raw_pool.close which is undefined-behavior on asyncpg.
    """
    raw_pool = MagicMock()
    raw_pool.close = AsyncMock()

    wrapped = ExecutorPool(raw_pool)
    await wrapped.close()
    raw_pool.close.assert_called_once()

    # Second call must not invoke raw_pool.close again.
    await wrapped.close()
    raw_pool.close.assert_called_once()
    assert wrapped._closed_flag is True


@pytest.mark.asyncio
async def test_close_returns_when_raw_pool_close_hangs():
    """If raw_pool.close() hangs forever (executor loop wedged scenario,
    observed live 2026-04-27 → asyncpg "Pool.close() is taking over 60
    seconds"), ExecutorPool.close() must still return within the bounded
    timeout so the postgres backend's recovery path can release
    _init_lock and proceed with slow-path recreate.
    """
    hang_started = threading.Event()

    async def hung_close():
        hang_started.set()
        # Sleep "forever" — a wait_for-bounded close must terminate us.
        await asyncio.sleep(3600)

    raw_pool = MagicMock()
    raw_pool.close = hung_close

    wrapped = ExecutorPool(raw_pool)

    # Shrink production 10s timeout to 0.5s for fast test.
    import src.db.executor_pool as ep_mod
    original_close_timeout = ep_mod.CLOSE_TIMEOUT_SECONDS
    ep_mod.CLOSE_TIMEOUT_SECONDS = 0.5
    try:
        # close() must return within ~0.5s (close timeout) + 2s (thread
        # join) + slack. If the wedge isn't bounded, this hangs.
        await asyncio.wait_for(wrapped.close(), timeout=5.0)
    finally:
        ep_mod.CLOSE_TIMEOUT_SECONDS = original_close_timeout

    assert wrapped._closed_flag is True
    assert hang_started.is_set(), (
        "raw_pool.close was never scheduled — wedged-loop test invalid"
    )
