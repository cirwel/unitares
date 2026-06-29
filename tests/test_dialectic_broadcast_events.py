"""Contract for the dialectic lifecycle broadcast events (#1167 Ask 1).

`_emit_dialectic_event` is the single guarded emit point the dialectic handlers
use to surface session lifecycle to the broadcaster firehose (and thus the
dashboard / Phoenix dialectic pane, which subscribe to any ``dialectic_*`` event).

Two invariants matter and are pinned here:
  1. The event reaches `broadcaster_instance.broadcast_event` with the ratified
     type, the paused agent as `agent_id`, and a payload that carries
     `session_id` + `awaiting_facilitation` (what the surface badges/sorts on).
  2. It is telemetry: a failing broadcaster must NEVER raise into the resolution
     path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.dialectic.handlers import _emit_dialectic_event, _emit_phase_changed


def _fake_session(**over):
    base = dict(
        session_id="sess-123",
        phase=SimpleNamespace(value="resolved"),
        topic="should we ship X",
        session_type="discovery",
        awaiting_facilitation=False,
        paused_agent_id="agent-abc",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_emit_passes_type_agent_and_payload(monkeypatch):
    captured = {}

    async def _capture(event_type, agent_id=None, payload=None):
        captured.update(event_type=event_type, agent_id=agent_id, payload=payload)

    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event",
        AsyncMock(side_effect=_capture),
    )

    await _emit_dialectic_event(
        "dialectic_resolved", _fake_session(), action="resume"
    )

    assert captured["event_type"] == "dialectic_resolved"
    assert captured["agent_id"] == "agent-abc"  # the paused agent, not the reviewer
    payload = captured["payload"]
    assert payload["session_id"] == "sess-123"
    assert payload["phase"] == "resolved"  # enum-like .value unwrapped
    assert payload["action"] == "resume"  # extra kwargs threaded through
    assert payload["awaiting_facilitation"] is False


@pytest.mark.asyncio
async def test_facilitation_event_carries_reason(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event",
        AsyncMock(side_effect=lambda *a, **k: captured.update(k, event_type=a[0])),
    )

    await _emit_dialectic_event(
        "dialectic_facilitation_needed",
        _fake_session(awaiting_facilitation=True),
        reason="reviewer_stuck",
    )

    assert captured["event_type"] == "dialectic_facilitation_needed"
    assert captured["payload"]["reason"] == "reviewer_stuck"
    assert captured["payload"]["awaiting_facilitation"] is True


@pytest.mark.asyncio
async def test_emit_never_raises_when_broadcaster_fails(monkeypatch):
    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event",
        AsyncMock(side_effect=RuntimeError("ws down")),
    )

    # Must swallow — telemetry cannot break the resolution path.
    await _emit_dialectic_event("dialectic_opened", _fake_session())


@pytest.mark.asyncio
async def test_emit_tolerates_a_minimal_session(monkeypatch):
    """Defensive getattrs: a session missing optional fields must not crash."""
    captured = {}
    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event",
        AsyncMock(side_effect=lambda *a, **k: captured.update(k, event_type=a[0])),
    )

    await _emit_dialectic_event("dialectic_opened", SimpleNamespace(session_id="s1"))

    assert captured["event_type"] == "dialectic_opened"
    assert captured["payload"]["session_id"] == "s1"
    assert captured["agent_id"] is None


@pytest.mark.asyncio
async def test_phase_changed_emits_on_transition(monkeypatch):
    """dialectic_phase_changed fires with from/to when the phase actually moved."""
    captured = {}
    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event",
        AsyncMock(side_effect=lambda *a, **k: captured.update(k, event_type=a[0])),
    )

    session = _fake_session(phase=SimpleNamespace(value="antithesis"))
    await _emit_phase_changed(session, "thesis")

    assert captured["event_type"] == "dialectic_phase_changed"
    assert captured["payload"]["from_phase"] == "thesis"
    assert captured["payload"]["to_phase"] == "antithesis"


@pytest.mark.asyncio
async def test_phase_changed_noop_when_phase_unchanged(monkeypatch):
    """No event when the phase did not move (e.g. another synthesis round)."""
    mock = AsyncMock()
    monkeypatch.setattr(
        "src.mcp_handlers.dialectic.handlers.broadcaster_instance.broadcast_event", mock
    )

    session = _fake_session(phase=SimpleNamespace(value="synthesis"))
    await _emit_phase_changed(session, "synthesis")

    mock.assert_not_awaited()
