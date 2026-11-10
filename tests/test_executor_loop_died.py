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
