"""
Regression tests for the idempotent terminal-transition guard in
DialecticDB.resolve_session (council 2026-06-28, "B-4").

The guard makes SYNTHESIS->RESOLVED safe across *processes*: the conditional
UPDATE (`WHERE status NOT IN ('resolved','failed')`) refuses to overwrite a
session that is already terminal. This defends against a crash-recovery
re-drive or a second writer (the forthcoming BEAM session owner) clobbering a
committed resolution_json — something the in-process asyncio.Lock cannot do.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_db import DialecticDB


def _db_with_conn(conn):
    """Build a DialecticDB whose pool.acquire() yields `conn`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    # _pool_is_alive() reads getattr(pool, '_closed', False); a bare MagicMock
    # auto-creates a truthy `_closed`, which would trigger a backend refresh and
    # replace our mock. Pin it False so _ensure_pool() no-ops.
    pool._closed = False
    return DialecticDB(pool=pool)


@pytest.mark.asyncio
async def test_fresh_resolve_performs_transition():
    """A non-terminal session is resolved; the conditional UPDATE returns a row."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"session_id": "sess-1"})
    db = _db_with_conn(conn)

    ok = await db.resolve_session("sess-1", {"verdict": "ok"}, status="resolved")

    assert ok is True
    # Only the UPDATE...RETURNING runs on the happy path (no follow-up SELECT).
    assert conn.fetchrow.await_count == 1
    sql = conn.fetchrow.await_args_list[0].args[0]
    assert "NOT IN ('resolved', 'failed')" in sql, "guard predicate must be present"


@pytest.mark.asyncio
async def test_already_resolved_is_idempotent_noop():
    """Re-resolving an already-resolved session returns True without overwrite."""
    conn = MagicMock()
    # UPDATE...RETURNING matches 0 rows (guard blocked) -> None;
    # follow-up SELECT shows it is already in the requested terminal state.
    conn.fetchrow = AsyncMock(side_effect=[None, {"status": "resolved"}])
    db = _db_with_conn(conn)

    ok = await db.resolve_session("sess-1", {"verdict": "second"}, status="resolved")

    assert ok is True  # idempotent success, not a lie and not a failure
    assert conn.fetchrow.await_count == 2  # UPDATE (no row) + status SELECT


@pytest.mark.asyncio
async def test_conflicting_terminal_state_refuses_overwrite():
    """A failed session is NOT silently overwritten to resolved; returns False."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"status": "failed"}])
    db = _db_with_conn(conn)

    ok = await db.resolve_session("sess-1", {"verdict": "late"}, status="resolved")

    assert ok is False  # conflict surfaced, existing 'failed' resolution preserved
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_missing_session_returns_false():
    """Resolving a non-existent session_id returns False, not a phantom success."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    db = _db_with_conn(conn)

    ok = await db.resolve_session("ghost", {"verdict": "x"}, status="resolved")

    assert ok is False
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_failed_status_sets_failed_phase():
    """status='failed' writes phase='failed' (not hardcoded 'resolved')."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"session_id": "sess-1"})
    db = _db_with_conn(conn)

    ok = await db.resolve_session("sess-1", {"reason": "stuck"}, status="failed")

    assert ok is True
    args = conn.fetchrow.await_args_list[0].args
    # args: (sql, status, phase, resolution_json, session_id)
    assert args[1] == "failed" and args[2] == "failed"
