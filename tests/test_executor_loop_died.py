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
