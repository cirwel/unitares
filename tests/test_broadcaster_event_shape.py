"""Pin the WS-receive payload shape for governance events.

The dashboard's categorical panels (Agents grid, Timeline, Residents)
dispatch on `data.type` and `data.agent_id` at the top level — see the
WS client `dashboard/redesign/ws.js` and the section reloads it triggers.
If a refactor ever shoved those fields under a nested `payload` key, the
dashboard would silently stop reflecting lifecycle changes between polls.

These tests lock in the contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.broadcaster import EISVBroadcaster


@pytest.mark.asyncio
async def test_lifecycle_event_top_level_shape_for_dashboard(monkeypatch):
    """`broadcast_event` must emit `{type, agent_id, timestamp, ...payload}`
    at the top level so dashboard JS can match agent cards by `agent_id`."""
    broadcaster = EISVBroadcaster()
    sent = []
    broadcaster._send_to_clients = AsyncMock(side_effect=lambda p: sent.append(p))
    # Avoid persisting to audit.events from a unit test — that path would
    # try to schedule a task on the running loop and touch real DB code.
    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task",
        lambda coro, name=None: coro.close() or AsyncMock(),
    )

    await broadcaster.broadcast_event(
        event_type="lifecycle_paused",
        agent_id="abc-123",
        payload={"reason": "EI imbalance", "event": "paused"},
    )

    assert len(sent) == 1
    event = sent[0]
    # Dashboard contract — flat keys at the top level.
    assert event["type"] == "lifecycle_paused"
    assert event["agent_id"] == "abc-123"
    assert "timestamp" in event
    # Payload fields are flattened, not nested under `payload`.
    assert event["reason"] == "EI imbalance"
    assert event["event"] == "paused"
    assert "payload" not in event


@pytest.mark.asyncio
async def test_event_with_no_agent_id_still_carries_field(monkeypatch):
    """Some events (e.g. fleet-wide) have no agent_id. The field must be
    present (as None) so consumers can null-check without `in event`."""
    broadcaster = EISVBroadcaster()
    sent = []
    broadcaster._send_to_clients = AsyncMock(side_effect=lambda p: sent.append(p))
    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task",
        lambda coro, name=None: coro.close() or AsyncMock(),
    )

    await broadcaster.broadcast_event(
        event_type="circuit_breaker_trip",
        agent_id=None,
        payload={"breaker": "steward.pi_sync"},
    )

    assert len(sent) == 1
    event = sent[0]
    assert event["type"] == "circuit_breaker_trip"
    assert "agent_id" in event
    assert event["agent_id"] is None
