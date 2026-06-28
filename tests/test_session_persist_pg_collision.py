"""Regression tests for the PG-layer session-collision observability fix.

`ensure_agent_persisted` calls `db.create_session`, which uses
`ON CONFLICT (session_id) DO NOTHING` and returns False when a row for the
session_key already exists. Previously the return value was ignored, so a
session_key already bound to a *different* identity at the PG layer (the S21-a
ghost-fork shape, durable layer) was silent.

The fix surfaces that collision via an [S21A_PG_SESSION_COLLISION] warning
(observability only — control flow is unchanged; the agent_uuid is decided
upstream). These tests assert the warning fires on a divergent existing
binding and stays quiet on a same-identity re-persist or a fresh insert.

See docs/proposals/redis-retirement-phase-1-plan.md (prep fix).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity import persistence


_AGENT_UUID = "11111111-1111-4111-8111-111111111111"
_OTHER_UUID = "22222222-2222-4222-8222-222222222222"
_SESSION_KEY = "agent-111111111111"


def _make_db(*, create_session_result: bool, existing_session_agent_id):
    """DB mock that drives ensure_agent_persisted to the create_session call.

    get_identity: first None (not-yet-persisted check), then the upserted row
    so the create_session branch is reached.
    """
    db = AsyncMock()
    db.get_identity = AsyncMock(side_effect=[
        None,  # line ~474 not-yet-persisted check
        SimpleNamespace(identity_id=42, metadata={}),  # line ~576 re-fetch
    ])
    db.get_agent = AsyncMock(return_value=None)
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.create_session = AsyncMock(return_value=create_session_result)
    db.get_session = AsyncMock(
        return_value=(
            SimpleNamespace(agent_id=existing_session_agent_id)
            if existing_session_agent_id is not None
            else None
        )
    )
    return db


async def _run(db):
    # _redis_cache=False makes _get_redis() return None -> skips Redis hydration.
    with patch.object(persistence, "_redis_cache", False), \
         patch.object(persistence, "get_db", return_value=db):
        return await persistence.ensure_agent_persisted(_AGENT_UUID, _SESSION_KEY)


@pytest.mark.asyncio
async def test_divergent_pg_session_logs_collision(caplog):
    """create_session returns False AND the existing row maps to a different
    UUID -> [S21A_PG_SESSION_COLLISION] warning fires."""
    db = _make_db(create_session_result=False, existing_session_agent_id=_OTHER_UUID)
    with caplog.at_level("WARNING"):
        await _run(db)
    db.get_session.assert_awaited_once_with(_SESSION_KEY)
    assert any("S21A_PG_SESSION_COLLISION" in r.message for r in caplog.records), \
        "divergent PG session binding must emit the collision warning"


@pytest.mark.asyncio
async def test_same_identity_repersist_is_quiet(caplog):
    """create_session returns False but the existing row maps to the SAME
    UUID -> benign re-persist, no warning."""
    db = _make_db(create_session_result=False, existing_session_agent_id=_AGENT_UUID)
    with caplog.at_level("WARNING"):
        await _run(db)
    assert not any("S21A_PG_SESSION_COLLISION" in r.message for r in caplog.records), \
        "same-identity re-persist must not warn"


@pytest.mark.asyncio
async def test_fresh_insert_skips_collision_check(caplog):
    """create_session returns True (fresh insert) -> no get_session probe,
    no warning."""
    db = _make_db(create_session_result=True, existing_session_agent_id=None)
    with caplog.at_level("WARNING"):
        await _run(db)
    db.get_session.assert_not_awaited()
    assert not any("S21A_PG_SESSION_COLLISION" in r.message for r in caplog.records)
