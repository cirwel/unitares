"""executor_loop_died.{premature_return,uncaught} — Wave 0 follow-up.

Council on the original §2.executor_loop scoping (in PR #369's reshape) reframed
the failure class: `ExecutorPool._run_loop` is a thread runner calling
`loop.run_forever()` — there is no main coroutine to receive cancellation.
The honest surface is a try/except/finally around `run_forever()` itself.

Two sub-types:
  - `coordination_failure.executor_loop_died.uncaught`         — run_forever() raised
  - `coordination_failure.executor_loop_died.premature_return` — run_forever() returned
                                                                 without operator-initiated close

The supervisor ought never see this fire under normal operation; if it does,
the pool is structurally broken and the operator needs to know.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


UNCAUGHT_EVENT_TYPE = "coordination_failure.executor_loop_died.uncaught"
PREMATURE_RETURN_EVENT_TYPE = "coordination_failure.executor_loop_died.premature_return"


def _make_bare_pool(*, closed_flag: bool):
    """Construct an ExecutorPool-shaped instance bypassing the thread-spawning
    constructor so we can drive `_run_loop` directly in the test thread."""
    from src.db.executor_pool import ExecutorPool

    pool = ExecutorPool.__new__(ExecutorPool)
    pool._loop = MagicMock(spec=asyncio.AbstractEventLoop)
    pool._loop_ready = threading.Event()
    pool._closed_flag = closed_flag
    return pool


def test_run_forever_raises_emits_uncaught():
    """An uncaught exception inside `loop.run_forever()` MUST emit
    `executor_loop_died.uncaught` and re-raise so the daemon thread dies
    visibly rather than silently."""
    pool = _make_bare_pool(closed_flag=False)
    pool._loop.run_forever = MagicMock(side_effect=RuntimeError("loop crashed"))

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        with pytest.raises(RuntimeError, match="loop crashed"):
            pool._run_loop()

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["service"] == "governance_mcp"
    assert kwargs["event_type"] == UNCAUGHT_EVENT_TYPE
    assert kwargs["payload"]["error_class"] == "RuntimeError"
    assert "incident_id" in kwargs["payload"]
    assert len(kwargs["payload"]["incident_id"]) == 36
    # The mocked run_forever never executes the scheduled readiness callback.
    # A caller must not be told the loop is ready before it actually runs.
    assert not pool._loop_ready.is_set()


def test_loop_ready_is_signalled_only_after_loop_is_running():
    """The creation waiter is released by the live loop, not its thread prologue."""
    from src.db.executor_pool import ExecutorPool

    pool = ExecutorPool.__new__(ExecutorPool)
    pool._loop = asyncio.new_event_loop()
    pool._loop_ready = threading.Event()
    pool._closed_flag = False
    thread = threading.Thread(target=pool._run_loop, daemon=True)
    thread.start()
    try:
        assert pool._loop_ready.wait(timeout=1.0)
        assert pool._loop.is_running()
    finally:
        pool._closed_flag = True
        pool._loop.call_soon_threadsafe(pool._loop.stop)
        thread.join(timeout=1.0)
        pool._loop.close()


def test_run_forever_returns_without_close_emits_premature_return():
    """`loop.run_forever()` returning while `_closed_flag` is still False means
    the loop exited without operator-initiated shutdown — structural bug
    worth flagging."""
    pool = _make_bare_pool(closed_flag=False)
    pool._loop.run_forever = MagicMock(return_value=None)  # returns immediately

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        pool._run_loop()  # should NOT raise

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["event_type"] == PREMATURE_RETURN_EVENT_TYPE
    # No specific exception class — the loop just returned
    assert kwargs["payload"].get("error_class") is None
    assert "incident_id" in kwargs["payload"]


def test_normal_close_does_not_emit():
    """When the operator calls `close()` (which sets `_closed_flag = True`
    before stopping the loop), `run_forever()` returning is the EXPECTED
    teardown — no emit."""
    pool = _make_bare_pool(closed_flag=True)
    pool._loop.run_forever = MagicMock(return_value=None)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        pool._run_loop()

    mock_emit.assert_not_called()


def test_emit_failure_does_not_swallow_uncaught_exception():
    """Defense-in-depth: if the emit itself raises, the original RuntimeError
    from `run_forever()` MUST still propagate. Observability must not mask
    the real bug."""
    pool = _make_bare_pool(closed_flag=False)
    pool._loop.run_forever = MagicMock(side_effect=RuntimeError("loop crashed"))

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync",
        side_effect=ValueError("emit blew up"),
    ):
        # Original RuntimeError propagates, NOT ValueError from emit
        with pytest.raises(RuntimeError, match="loop crashed"):
            pool._run_loop()


@pytest.mark.asyncio
async def test_executor_pool_startup_timeout_cleans_up_thread_and_loop(monkeypatch):
    """If `_loop_ready.wait` times out during `__init__` or `create()`,
    `ExecutorPool` raises `ConnectionError`:
    - When the thread is genuinely stalled past `THREAD_JOIN_TIMEOUT_SECONDS`,
      `_abort_startup*` avoids closing `loop` underneath the live thread, and
      the queued `loop.stop()` terminates `run_forever()` on its first tick
      without emitting `executor_loop_died.premature_return`.
    - When the thread has already exited, `_abort_startup*` closes `loop`."""
    import src.db.executor_pool as ep_mod
    from src.db.executor_pool import ExecutorPool

    created_loops = []
    spawned_threads = []
    real_new_event_loop = asyncio.new_event_loop
    real_run_loop = ExecutorPool._run_loop

    def tracking_new_event_loop():
        loop = real_new_event_loop()
        created_loops.append(loop)
        return loop

    release_stalled = threading.Event()

    def delayed_real_run_loop(self):
        spawned_threads.append(threading.current_thread())
        release_stalled.wait(timeout=2.0)
        real_run_loop(self)

    monkeypatch.setattr(ep_mod.asyncio, "new_event_loop", tracking_new_event_loop)
    monkeypatch.setattr(ep_mod, "LOOP_START_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(ep_mod, "THREAD_JOIN_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(ExecutorPool, "_run_loop", delayed_real_run_loop)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        # 1. Truly stalled threads (still alive when join times out):
        with pytest.raises(ConnectionError, match="failed to start"):
            ExecutorPool(MagicMock())

        with pytest.raises(ConnectionError, match="failed to start"):
            await ExecutorPool.create(lambda: asyncio.sleep(0))

        assert len(created_loops) == 2
        # Loops must NOT be closed while the stalled threads are still alive.
        for loop in created_loops:
            assert not loop.is_closed()

        # Releasing the stalled threads runs real `_run_loop`, which immediately
        # processes the queued `loop.stop()` and exits cleanly with zero emits.
        release_stalled.set()
        for thread in spawned_threads:
            thread.join(timeout=1.0)
            assert not thread.is_alive()
        for loop in created_loops:
            assert not loop.is_running()
            loop.close()

        mock_emit.assert_not_called()

    # 2. Thread already dead before join completes -> loop is closed immediately:
    monkeypatch.setattr(ExecutorPool, "_run_loop", lambda self: None)
    with pytest.raises(ConnectionError, match="failed to start"):
        ExecutorPool(MagicMock())
    assert created_loops[-1].is_closed()


@pytest.mark.asyncio
async def test_executor_pool_create_factory_failure_cleans_up_thread_and_loop():
    """If `create_pool_factory` raises inside `ExecutorPool.create()`, the
    spawned executor thread and event loop are stopped and closed so retries
    from `PostgresBackend._create_pool` do not leak threads."""
    from src.db.executor_pool import ExecutorPool

    seen = {}

    async def failing_pool():
        seen["loop"] = asyncio.get_running_loop()
        seen["thread"] = threading.current_thread()
        raise OSError("postgres unreachable")

    with pytest.raises(OSError, match="postgres unreachable"):
        await ExecutorPool.create(failing_pool)

    assert not seen["thread"].is_alive()
    assert not seen["loop"].is_running()
    assert seen["loop"].is_closed()


@pytest.mark.asyncio
async def test_executor_pool_create_cancelled_mid_flight_unwinds_factory_before_loop_stop():
    """Cancelling a caller task awaiting `ExecutorPool.create()` mid-creation
    cancels and drains the factory task on the executor loop before stopping and
    closing that loop."""
    from src.db.executor_pool import ExecutorPool

    factory_started = threading.Event()
    seen = {"unwound": False}

    async def slow_pool():
        seen["loop"] = asyncio.get_running_loop()
        seen["thread"] = threading.current_thread()
        factory_started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0)
            seen["unwound"] = True

    task = asyncio.create_task(ExecutorPool.create(slow_pool))
    await asyncio.to_thread(factory_started.wait, 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen["unwound"] is True
    assert not seen["thread"].is_alive()
    assert not seen["loop"].is_running()
    assert seen["loop"].is_closed()


@pytest.mark.asyncio
async def test_executor_pool_create_cancelled_wedged_cleanup_stops_and_closes_loop(monkeypatch):
    """If a cancelled `create_pool_factory` hangs inside its `finally` cleanup
    past `CLOSE_TIMEOUT_SECONDS`, `_abort_startup` force-stops the executor loop,
    joins the thread, and closes the loop instead of leaving it unclosed."""
    import src.db.executor_pool as ep_mod
    from src.db.executor_pool import ExecutorPool

    monkeypatch.setattr(ep_mod, "CLOSE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(ep_mod, "THREAD_JOIN_TIMEOUT_SECONDS", 0.2)

    factory_started = threading.Event()
    seen = {}

    async def wedged_cleanup_pool():
        seen["loop"] = asyncio.get_running_loop()
        seen["thread"] = threading.current_thread()
        factory_started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(60)

    task = asyncio.create_task(ExecutorPool.create(wedged_cleanup_pool))
    await asyncio.to_thread(factory_started.wait, 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not seen["thread"].is_alive()
    assert not seen["loop"].is_running()
    assert seen["loop"].is_closed()


@pytest.mark.asyncio
async def test_await_on_loop_converts_runtime_error_on_closed_loop_without_warning():
    """If a loop stops/closes between `is_running()` and `run_coroutine_threadsafe`,
    `_await_on_loop` closes the coroutine and raises `ConnectionError`."""
    import gc
    import warnings

    from src.db.executor_pool import _await_on_loop

    # Simulate a stale True return from `loop.is_running()` right before `close()`.
    class _StaleRunningLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, *_a, **_k):
            raise RuntimeError("Event loop is closed")

    async def sample():
        return 42

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        with pytest.raises(ConnectionError, match="not running"):
            await _await_on_loop(sample(), _StaleRunningLoop())
        gc.collect()
    assert not [w for w in caught if "was never awaited" in str(w.message)]


@pytest.mark.asyncio
async def test_await_on_loop_inflight_fails_promptly_when_loop_stops():
    """An inflight call queued right as `_run_loop` stops fails promptly with
    `ConnectionError` instead of hanging forever."""
    from src.db.executor_pool import ExecutorPool, _await_on_loop

    pool = ExecutorPool(MagicMock())
    started = threading.Event()

    async def never_finishes():
        started.set()
        await asyncio.sleep(3600)

    call_task = asyncio.create_task(
        _await_on_loop(never_finishes, pool._loop, **pool._dispatch_kwargs())
    )
    await asyncio.to_thread(started.wait, 2.0)

    pool._closed_flag = True
    with pool._dispatch_lock:
        pool._loop_accepting = False
    pool._loop.call_soon_threadsafe(pool._loop.stop)
    pool._thread.join(timeout=2.0)

    with pytest.raises(ConnectionError, match="stopped"):
        await asyncio.wait_for(call_task, timeout=2.0)


@pytest.mark.asyncio
async def test_concurrent_acquire_and_queries_racing_pool_close_never_hang_or_raise_runtime_error():
    """Concurrent DB calls racing `ExecutorPool.close()` either complete cleanly
    or fail closed with `ConnectionError`, never `RuntimeError('Event loop is closed')`."""
    import gc
    import warnings

    from src.db.executor_pool import ExecutorPool

    class _FakeRawConn:
        async def fetchval(self, *_a, **_k):
            await asyncio.sleep(0.002)
            return 1

    class _FakeAcquireCtx:
        async def __aenter__(self):
            return _FakeRawConn()

        async def __aexit__(self, *_exc):
            return False

    raw_pool = MagicMock()
    raw_pool.acquire = MagicMock(side_effect=lambda **_kw: _FakeAcquireCtx())

    async def _raw_close():
        await asyncio.sleep(0.01)

    raw_pool.close = _raw_close
    pool = ExecutorPool(raw_pool)

    async def worker():
        for _ in range(15):
            try:
                async with pool.acquire() as conn:
                    val = await conn.fetchval("SELECT 1")
                    assert val == 1
            except ConnectionError:
                return
            await asyncio.sleep(0.001)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        workers = [asyncio.create_task(worker()) for _ in range(6)]
        await asyncio.sleep(0.008)
        close_task = asyncio.create_task(pool.close())
        await asyncio.wait_for(asyncio.gather(*workers, close_task), timeout=5.0)
        gc.collect()

    assert not [w for w in caught if "was never awaited" in str(w.message)]
