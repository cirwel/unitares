"""
ExecutorPool — wraps asyncpg.Pool so DB operations run on a dedicated
background thread with its own asyncio event loop.

Why: . The MCP SDK's anyio
task group conflicts with asyncpg/Redis cancellation semantics. Running
asyncpg coroutines on a separate event loop (in a separate thread) means
the anyio context never sees an asyncpg await — only a future from the
caller's loop, which anyio handles cleanly.

Caller surface is unchanged: handlers still write
    async with db.acquire() as conn:
        await conn.fetchval(...)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Bounded timeout for ExecutorPool.close(). If the executor loop is wedged
# (asyncpg released connections but DISCARD ALL responses unread, observed
# 2026-04-27), close() must still return so the postgres backend's recovery
# path can release _init_lock and proceed. Module-level so tests can patch.
CLOSE_TIMEOUT_SECONDS = 10.0
THREAD_JOIN_TIMEOUT_SECONDS = 2.0


def _emit_executor_loop_died(sub_type: str, *, error_class: str | None) -> None:
    """Failure-safe emit for `coordination_failure.executor_loop_died.<sub_type>`.

    Wave 0 follow-up to PR #369's reshape: the original §2.executor_loop
    scoping under `anyio_cancellation` was structurally wrong (no main
    coroutine to receive cancel; CPython #105836 prevents anyio teardown
    from propagating cancel across `run_coroutine_threadsafe`). The honest
    failure class for this loop is "the loop died" — premature `run_forever()`
    return or uncaught exception in `_run_loop` — a different family.

    `sub_type` is one of: "uncaught", "premature_return".

    Same failure-safety contract as the postgres_backend coord-failure
    helpers — wraps the inner emit in try/except so neither an ImportError
    nor an emit-side bug can mask the original exception (`raise` follows
    in `_run_loop` for the uncaught path)."""
    try:
        from uuid import uuid4

        from src.coordination_failure_emit import emit_coordination_failure_sync

        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type=f"coordination_failure.executor_loop_died.{sub_type}",
            payload={
                "error_class": error_class,
                "incident_id": str(uuid4()),
            },
            agent_id=None,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-events] executor_loop_died emit raised — original "
            "exception (if any) will still propagate: %r",
            exc,
        )


async def _await_on_loop(target: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """Schedule on `loop` (a different thread's loop) and await the result.

    Accepts either:
    - A **callable** returning a coroutine/awaitable — called *inside* the
      executor loop so any Futures it creates are loop-bound correctly.
      **Use this form for asyncpg ops** (asyncpg internals capture the
      running loop when creating Futures).
    - A coroutine — passed straight to ``run_coroutine_threadsafe``.
      Safe only for plain coroutines that don't internally create Futures
      bound to the calling loop.
    """
    if callable(target):
        async def _call_on_executor():
            result = target()
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                return await result
            return result
        coro = _call_on_executor()
    elif asyncio.iscoroutine(target):
        coro = target
    else:
        async def _wrap():
            return await target
        coro = _wrap()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return await asyncio.wrap_future(future)


class _Transaction:
    """
    Wraps an asyncpg transaction so __aenter__/__aexit__ both round-trip
    to the executor loop. asyncpg connections are loop-bound — a transaction
    started on one loop and committed on another silently corrupts.
    """

    def __init__(self, raw_txn: Any, loop: asyncio.AbstractEventLoop):
        self._raw = raw_txn
        self._loop = loop

    async def __aenter__(self) -> Any:
        return await _await_on_loop(lambda: self._raw.__aenter__(), self._loop)

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> Any:
        return await _await_on_loop(
            lambda: self._raw.__aexit__(exc_type, exc_val, exc_tb), self._loop
        )

    async def start(self) -> Any:
        return await _await_on_loop(lambda: self._raw.start(), self._loop)

    async def commit(self) -> Any:
        return await _await_on_loop(lambda: self._raw.commit(), self._loop)

    async def rollback(self) -> Any:
        return await _await_on_loop(lambda: self._raw.rollback(), self._loop)


class _Connection:
    """Wraps an asyncpg Connection, dispatching all calls to the executor loop."""

    def __init__(self, raw_conn: Any, loop: asyncio.AbstractEventLoop):
        self._raw = raw_conn
        self._loop = loop

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_on_loop(lambda: self._raw.fetchval(*args, **kwargs), self._loop)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_on_loop(lambda: self._raw.fetch(*args, **kwargs), self._loop)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_on_loop(lambda: self._raw.fetchrow(*args, **kwargs), self._loop)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_on_loop(lambda: self._raw.execute(*args, **kwargs), self._loop)

    async def executemany(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_on_loop(lambda: self._raw.executemany(*args, **kwargs), self._loop)

    def transaction(self, *args: Any, **kwargs: Any) -> _Transaction:
        # transaction() is a sync method on asyncpg.Connection that returns
        # a Transaction object — the actual BEGIN/COMMIT happen in __aenter__/
        # __aexit__, both of which must round-trip to the executor loop.
        return _Transaction(self._raw.transaction(*args, **kwargs), self._loop)

    async def close(self) -> None:
        return await _await_on_loop(lambda: self._raw.close(), self._loop)


class _AcquireContext:
    """
    Mirrors asyncpg's PoolAcquireContext: BOTH awaitable AND an async context
    manager. `async with pool.acquire(): ...` auto-releases. `await pool.acquire()`
    returns the connection directly — caller must `pool.release(conn)` later.
    """

    def __init__(self, raw_pool: Any, loop: asyncio.AbstractEventLoop, timeout: Any = None):
        self._raw_pool = raw_pool
        self._loop = loop
        self._timeout = timeout
        self._raw_acquire_ctx: Any = None
        self._raw_conn: Any = None

    def __await__(self):
        async def _direct_acquire():
            def _factory():
                kwargs = {} if self._timeout is None else {"timeout": self._timeout}
                return self._raw_pool.acquire(**kwargs)
            return await _await_on_loop(_factory, self._loop)

        raw_conn = yield from _direct_acquire().__await__()
        return _Connection(raw_conn, self._loop)

    async def __aenter__(self) -> _Connection:
        # The PoolAcquireContext is created on the executor loop AND its
        # __aenter__ is awaited there, so the connection comes back loop-bound
        # to the executor loop. Storing the ctx so __aexit__ can reuse it.
        def _enter_factory():
            kwargs = {} if self._timeout is None else {"timeout": self._timeout}
            self._raw_acquire_ctx = self._raw_pool.acquire(**kwargs)
            return self._raw_acquire_ctx.__aenter__()

        self._raw_conn = await _await_on_loop(_enter_factory, self._loop)
        return _Connection(self._raw_conn, self._loop)

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> Any:
        return await _await_on_loop(
            lambda: self._raw_acquire_ctx.__aexit__(exc_type, exc_val, exc_tb),
            self._loop,
        )


class ExecutorPool:
    """
    Wraps an asyncpg.Pool. All DB operations route through a dedicated
    background thread that owns its own asyncio event loop.
    """

    def __init__(self, raw_pool: Any):
        # Direct constructor: caller has already-created pool (mocks, tests).
        # Production code should use `await ExecutorPool.create(coro_factory)`
        # so the asyncpg pool is created on the executor loop — asyncpg
        # connections are loop-bound (architect's per-thread pinning).
        self._raw_pool = raw_pool
        # Close coordination: lock serializes concurrent close() callers;
        # done event lets the second caller AWAIT the first's completion
        # rather than return prematurely while raw_pool.close() is still
        # in flight (council-found bug pre-shipping).
        self._close_lock = asyncio.Lock()
        self._close_done = asyncio.Event()
        self._closed_flag = False
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ExecutorPool-loop",
            daemon=True,
        )
        self._thread.start()
        self._loop_ready.wait(timeout=5.0)

    @classmethod
    async def create(cls, create_pool_factory: Any) -> "ExecutorPool":
        """Create the asyncpg pool ON the executor loop.

        ``create_pool_factory`` is a callable returning the awaitable from
        ``asyncpg.create_pool(...)`` — calling it inside the executor loop
        means the pool and all its connections are bound to that loop.
        """
        instance = cls.__new__(cls)
        instance._close_lock = asyncio.Lock()
        instance._close_done = asyncio.Event()
        instance._closed_flag = False
        instance._loop = asyncio.new_event_loop()
        instance._loop_ready = threading.Event()
        instance._thread = threading.Thread(
            target=instance._run_loop,
            name="ExecutorPool-loop",
            daemon=True,
        )
        instance._thread.start()
        instance._loop_ready.wait(timeout=5.0)
        # Pass the factory (not the result) so it's called *inside* the
        # executor loop — asyncpg.create_pool's Futures must be loop-bound
        # to the executor loop.
        instance._raw_pool = await _await_on_loop(create_pool_factory, instance._loop)
        return instance

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        except BaseException as exc:
            # Uncaught exception in the executor loop thread. Daemon threads
            # die silently when the parent process exits, so without this
            # emit a structural failure here would only surface as missing
            # behavior elsewhere. Re-raise so the thread dies visibly.
            _emit_executor_loop_died("uncaught", error_class=type(exc).__name__)
            raise
        else:
            # `run_forever()` returned normally. If the operator initiated
            # shutdown via `close()`, `_closed_flag` is True and this is
            # expected teardown. Otherwise the loop exited unexpectedly —
            # that's a structural bug worth flagging.
            if not self._closed_flag:
                _emit_executor_loop_died("premature_return", error_class=None)

    def acquire(self, timeout: Any = None) -> _AcquireContext:
        return _AcquireContext(self._raw_pool, self._loop, timeout=timeout)

    async def release(self, conn: _Connection) -> Any:
        # Pair to `await pool.acquire()`. Forwards to raw pool with the
        # underlying asyncpg connection (postgres_backend.py:206).
        return await _await_on_loop(lambda: self._raw_pool.release(conn._raw), self._loop)

    @property
    def _closed(self) -> Any:
        # Mirror asyncpg's internal `_closed` attribute. dialectic_db.py:55
        # reads this to test pool liveness.
        return self._raw_pool._closed

    def get_size(self) -> Any:
        return self._raw_pool.get_size()

    def get_idle_size(self) -> Any:
        return self._raw_pool.get_idle_size()

    def get_max_size(self) -> Any:
        return self._raw_pool.get_max_size()

    async def close(self) -> None:
        # Concurrent close() callers must serialize, not race the
        # check-then-set on _closed_flag. Recovery path is gated by
        # postgres_backend._init_lock, but daemon-shutdown can call
        # close() concurrently with recovery — that's the race the
        # lock guards against. Second caller awaits the done-event so
        # they see a fully-closed pool, not a half-closed one (council).
        async with self._close_lock:
            if self._closed_flag:
                # Already closed (or close in progress and done). Wait
                # for completion before returning so caller sees a
                # consistent state.
                await self._close_done.wait()
                return
            self._closed_flag = True

        # Teardown order matters (architect): close the raw pool ON
        # the executor loop (asyncpg connections are loop-bound),
        # THEN stop the loop, THEN join the thread.
        #
        # Bounded wait: if the executor loop is wedged (asyncpg
        # released connections but PG-side responses unread —
        # observed 2026-04-27), _await_on_loop hangs. wait_for
        # guarantees the caller (postgres_backend recovery path
        # holding _init_lock) returns within CLOSE_TIMEOUT_SECONDS.
        # If the loop never drains we cancel the orphaned task on
        # the executor loop (CPython #105836: wait_for cancellation
        # does NOT propagate across run_coroutine_threadsafe), then
        # force-stop and abandon. Daemon thread dies with process.
        _to_reraise: BaseException | None = None
        try:
            await asyncio.wait_for(
                _await_on_loop(
                    lambda: self._raw_pool.close(), self._loop,
                ),
                timeout=CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ExecutorPool.close() timed out — executor loop "
                "wedged; cancelling orphan task and abandoning thread."
            )
            self._cancel_orphan_tasks_on_executor_loop()
        except BaseException as e:
            # BaseException (CancelledError, KeyboardInterrupt,
            # GeneratorExit, Exception subclasses) must NOT bypass
            # teardown — the loop thread would keep running. Log,
            # capture, and fall through to finally. Re-raise after
            # teardown so callers still see cancellation/Ctrl-C.
            logger.warning(
                f"ExecutorPool.close() interrupted: "
                f"{type(e).__name__}: {e}"
            )
            _to_reraise = e
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            # Set the done event BEFORE the thread-join await. If this task is
            # cancelled at that await, _close_done.set() would be skipped, and
            # any concurrent close() caller blocked on _close_done.wait() would
            # hold _close_lock forever — reintroducing the same "hold forever"
            # failure mode this whole change was meant to fix.
            self._close_done.set()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._thread.join, THREAD_JOIN_TIMEOUT_SECONDS,
                )
            except BaseException:
                # CancelledError is a BaseException since 3.8; catch widely
                # so the thread-join best-effort doesn't strand callers.
                pass

        if _to_reraise is not None:
            raise _to_reraise

    def _cancel_orphan_tasks_on_executor_loop(self) -> None:
        """Cancel any pending tasks on the executor loop without awaiting.

        Called when wait_for(close()) timed out — wait_for cancels its
        own outer awaitable but the run_coroutine_threadsafe-scheduled
        coroutine on the executor loop keeps running (CPython #105836).
        Without this, raw_pool.close() runs to completion (or hangs
        forever) on a thread whose Python-side caller already moved on,
        producing "Task was destroyed but it is pending" warnings on
        eventual GC and tying up the executor thread.

        Best-effort: posts cancel() calls; doesn't await them. The
        finally block's loop.stop() will exit the loop after the
        cancellations land or after the next iteration.
        """
        def _cancel_all():
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
        try:
            self._loop.call_soon_threadsafe(_cancel_all)
        except Exception:
            pass
