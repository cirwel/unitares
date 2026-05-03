"""Tests for P005 false-positive suppression on context-managed acquires.

P005 fires on resource-leak shapes (acquire/cursor/connect/lock without a
matching release). When the call sits inside an `async with` (or plain
`with`) header, the context manager handles release on __aexit__, so the
finding is by construction a false positive. Caught 2026-04-24 when the
model flagged `async with db.acquire() as conn:` lines (KG entry
2026-04-24T02:01:05).
"""

from __future__ import annotations

import pytest

from agents.watcher.agent import _verify_finding_against_source
from agents.watcher.findings import Finding


def _make(line: int, pattern: str = "P005") -> Finding:
    return Finding(
        pattern=pattern,
        file="src/example.py",
        line=line,
        hint="resource leak",
        severity="medium",
        detected_at="2026-04-25T00:00:00Z",
        model_used="test",
    )


class TestP005ContextManagedSuppression:
    """`async with X.acquire()` and friends must drop — context manager releases."""

    def test_async_with_db_acquire_dropped(self):
        snippet = {1: "async with db.acquire() as conn:"}
        assert _verify_finding_against_source(_make(1), "", snippet) is False

    def test_async_with_pool_acquire_dropped(self):
        snippet = {5: "    async with pool.acquire() as conn:"}
        assert _verify_finding_against_source(_make(5), "", snippet) is False

    def test_async_with_redis_lock_dropped(self):
        snippet = {3: "async with redis.lock(name) as lock:"}
        assert _verify_finding_against_source(_make(3), "", snippet) is False

    def test_async_with_conn_cursor_dropped(self):
        snippet = {7: "async with conn.cursor() as cur:"}
        assert _verify_finding_against_source(_make(7), "", snippet) is False

    def test_plain_with_connect_dropped(self):
        snippet = {2: "with sqlite3.connect(path) as conn:"}
        assert _verify_finding_against_source(_make(2), "", snippet) is False


class TestP005AcquireWrapperSuppression:
    """Pass-through `acquire()` wrappers transfer ownership; they don't consume."""

    def test_pool_backend_acquire_passthrough_dropped(self):
        # Mirrors /tmp/r1_verify.py's PoolBackend.acquire wrapper that simply
        # returns the pool's acquire context manager to callers.
        snippet = {
            22: "class PoolBackend(PostgresBackend):",
            23: "    def __init__(self, pool):",
            24: "        self._pool = pool",
            25: "",
            26: "    def acquire(self):",
            27: "        return self._pool.acquire()",
        }
        assert _verify_finding_against_source(_make(27), "", snippet) is False

    def test_mismatched_passthrough_wrapper_name_kept(self):
        # Keep precision: only same-name factory/pass-through wrappers drop.
        snippet = {
            10: "def get_conn(self):",
            11: "    return self._pool.acquire()",
        }
        assert _verify_finding_against_source(_make(11), "", snippet) is True


class TestP005RealLeaksKept:
    """Bare acquires without a context manager must NOT be dropped here."""

    def test_bare_acquire_assignment_kept(self):
        # No context manager — must not be dropped by the new rule.
        # (Survives the required-token gate; goes through to remaining checks.)
        snippet = {4: "    conn = await db.acquire()"}
        assert _verify_finding_against_source(_make(4), "", snippet) is True

    def test_bare_cursor_assignment_kept(self):
        snippet = {6: "    cur = conn.cursor()"}
        assert _verify_finding_against_source(_make(6), "", snippet) is True

    def test_acquire_in_comment_dropped_by_existing_rule(self):
        # Comment lines are dropped by _looks_like_comment, not by us.
        snippet = {8: "    # remember to .acquire() the lock"}
        assert _verify_finding_against_source(_make(8), "", snippet) is False


class TestP005DropDoesNotLeakToOtherPatterns:
    """The new drop must only apply to P005."""

    def test_p001_with_acquire_on_line_not_affected(self):
        # P001 has its own required-token gate (`create_task(`), so an
        # `async with db.acquire()` line fails P001's required-token check
        # before reaching our new P005 branch.
        snippet = {1: "async with db.acquire() as conn:"}
        assert _verify_finding_against_source(_make(1, pattern="P001"), "", snippet) is False


class TestP005PreInitTryFinally:
    """`<var> = None; try: <var> = await X.connect(...)` must drop — manual
    release via finally with None-check is the canonical safe shape when the
    resource type has no async context-manager protocol (e.g. asyncpg)."""

    def test_basic_conn_none_then_try_acquire_dropped(self):
        # Mirrors the chronicler scrapers.py shape that refired P005 after the
        # 2026-04-25 fix moved acquire inside try.
        snippet = {
            40: "async def _run() -> float:",
            41: "conn = None",
            42: "try:",
            43: "conn = await asyncpg.connect(dsn)",
        }
        assert _verify_finding_against_source(_make(43), "", snippet) is False

    def test_pool_acquire_dropped(self):
        snippet = {
            10: "async def use_pool():",
            11: "client = None",
            12: "try:",
            13: "client = await pool.acquire()",
        }
        assert _verify_finding_against_source(_make(13), "", snippet) is False

    def test_cursor_dropped(self):
        snippet = {
            10: "async def use_cursor(conn):",
            11: "cur = None",
            12: "try:",
            13: "cur = await conn.cursor()",
        }
        assert _verify_finding_against_source(_make(13), "", snippet) is False

    def test_no_try_between_init_and_acquire_kept(self):
        # `<var> = None` then bare acquire (no try:) is NOT the safe pattern.
        snippet = {
            10: "async def f():",
            11: "conn = None",
            12: "conn = await db.acquire()",
        }
        assert _verify_finding_against_source(_make(12), "", snippet) is True

    def test_no_none_init_kept(self):
        # try: <var> = await ... without a preceding None-init is unsafe — if
        # the acquire raises, the var is unbound and `finally: await x.close()`
        # raises NameError, masking the original exception.
        snippet = {
            10: "async def f():",
            11: "try:",
            12: "conn = await db.acquire()",
        }
        assert _verify_finding_against_source(_make(12), "", snippet) is True

    def test_mismatched_var_name_kept(self):
        # None-init is for a different variable than the acquired one.
        snippet = {
            10: "async def f():",
            11: "other = None",
            12: "try:",
            13: "conn = await db.acquire()",
        }
        assert _verify_finding_against_source(_make(13), "", snippet) is True

    def test_function_boundary_stops_walk(self):
        # `<var> = None` lives in a different function — must not match.
        snippet = {
            5:  "async def other():",
            6:  "conn = None",
            7:  "    pass",
            10: "async def f():",
            11: "try:",
            12: "conn = await db.acquire()",
        }
        assert _verify_finding_against_source(_make(12), "", snippet) is True

    def test_blank_lines_and_comments_ignored_in_walk(self):
        snippet = {
            10: "async def f():",
            11: "conn = None",
            12: "",
            13: "# acquire and use",
            14: "try:",
            15: "conn = await db.acquire()",
        }
        assert _verify_finding_against_source(_make(15), "", snippet) is False

    def test_acquire_outside_try_dropped_by_acquire_then_try_filter(self):
        # `<var> = await X.acquire(); try:` is the canonical asyncpg idiom
        # under the policy from issue #268 — the cancel-between-acquire-
        # and-try window is theoretical and the operator's intent is
        # unambiguous, so the third P005 filter
        # (`_is_acquire_then_try_with_unconditional_close`) drops it.
        # The `conn = None` pre-init above is incidental — it does not
        # change the acquire-then-try shape on lines 12-13.
        snippet = {
            10: "async def f():",
            11: "conn = None",
            12: "conn = await db.acquire()",
            13: "try:",
            14: "    pass",
        }
        assert _verify_finding_against_source(_make(12), "", snippet) is False
