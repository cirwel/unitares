from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from src.db.mixins.audit import AuditMixin


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Backend(AuditMixin):
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


@pytest.mark.asyncio
async def test_agent_scoped_confidence_lookup_does_not_fallback_to_other_agents():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    backend = _Backend(conn)

    result = await backend.get_latest_confidence_before(
        before_ts=datetime(2026, 4, 12, tzinfo=timezone.utc),
        agent_id="agent-a",
    )

    assert result is None
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.call_args.args[0]
    assert "agent_id = $1" in sql


@pytest.mark.asyncio
async def test_global_confidence_lookup_still_works_without_agent_id():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"confidence": 0.73})
    backend = _Backend(conn)

    result = await backend.get_latest_confidence_before(
        before_ts=datetime(2026, 4, 12, tzinfo=timezone.utc),
        agent_id=None,
    )

    assert result == 0.73
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.call_args.args[0]
    assert "agent_id NOT IN ('system', 'eisv-sync-task')" in sql


@pytest.mark.asyncio
async def test_exact_event_id_query_normalizes_json_payload() -> None:
    event_id = uuid.uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "ts": datetime(2026, 8, 28, tzinfo=timezone.utc),
                "event_id": event_id,
                "agent_id": None,
                "session_id": None,
                "event_type": "infrastructure.canary",
                "confidence": 1.0,
                "payload": json.dumps({"nested": {"ok": True}}),
                "raw_hash": "a" * 64,
            }
        ]
    )
    backend = _Backend(conn)

    [event] = await backend.query_audit_events(event_id=str(event_id), limit=2)

    assert event.event_id == str(event_id)
    assert event.payload == {"nested": {"ok": True}}
    sql, *params = conn.fetch.await_args.args
    assert "event_id = $1" in sql
    assert params == [event_id, 2]


def test_exact_event_id_filter_rejects_invalid_uuid() -> None:
    with pytest.raises(ValueError):
        AuditMixin._audit_event_filters(event_id="not-a-uuid")


@pytest.mark.asyncio
async def test_public_audit_query_threads_event_id_and_complete_row(
    monkeypatch,
) -> None:
    from src.audit_db import query_audit_events_async
    from src.db.base import AuditEvent

    event_id = str(uuid.uuid4())
    backend = MagicMock()
    backend._pool = object()
    backend.query_audit_events = AsyncMock(
        return_value=[
            AuditEvent(
                ts=datetime(2026, 8, 28, tzinfo=timezone.utc),
                event_id=event_id,
                event_type="infrastructure.canary",
                agent_id=None,
                session_id=None,
                confidence=1.0,
                payload={"nested": {"ok": True}},
                raw_hash="b" * 64,
            )
        ]
    )
    monkeypatch.setattr("src.db.get_db", lambda: backend)

    [row] = await query_audit_events_async(event_id=event_id, limit=2)

    assert row["event_id"] == event_id
    assert row["session_id"] is None
    assert row["details"] == {"nested": {"ok": True}}
    assert row["raw_hash"] == "b" * 64
    backend.query_audit_events.assert_awaited_once_with(
        agent_id=None,
        event_type=None,
        event_types=None,
        start_time=None,
        end_time=None,
        limit=2,
        order="asc",
        event_id=event_id,
    )
