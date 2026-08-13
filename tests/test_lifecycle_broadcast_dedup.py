"""Regression tests for the lifecycle-event emission path.

Prior behaviour: every lifecycle transition wrote TWO audit.events rows —
one via broadcaster._persist_event (payload shape {"type": ..., "reason": ...})
and one via the direct append_audit_event_async call in
_emit_lifecycle_event (payload shape {"reason": ..., "event": ...}).

Under load (Steward's 5-min circuit-breaker trips) this doubled every
Discord alert and every dashboard row. Fix: broadcast is the single
audit path; the direct write now runs only as a fallback when the
broadcast is skipped or fails.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src import agent_metadata_model


@pytest.fixture
def captured_audit_and_broadcast(monkeypatch):
    """Stub the broadcaster and audit_db.append_audit_event_async.

    _emit_lifecycle_event schedules _emit onto the running event loop via
    create_tracked_task. We capture the coroutine so tests can await it
    deterministically.
    """
    import src.broadcaster as broadcaster_module
    import src.audit_db as audit_module

    broadcaster = AsyncMock()
    audit = AsyncMock()

    monkeypatch.setattr(broadcaster_module, "broadcaster_instance", broadcaster)
    monkeypatch.setattr(audit_module, "append_audit_event_async", audit)

    # Run scheduled tasks synchronously so the test can assert on them.
    scheduled = []

    def _fake_create_tracked_task(coro, name=None):
        scheduled.append(coro)
        return AsyncMock()

    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task", _fake_create_tracked_task
    )

    return broadcaster, audit, scheduled


@pytest.mark.asyncio
async def test_lifecycle_paused_emits_single_audit_row(captured_audit_and_broadcast):
    """Real agent: broadcast fires once, no direct audit write.

    broadcaster._persist_event (not stubbed here) handles the audit write
    via its own pipeline. From _emit_lifecycle_event's perspective, once
    the broadcast succeeds it MUST NOT also call append_audit_event_async
    directly — that was the source of the duplicate Discord alerts.
    """
    broadcaster, audit, scheduled = captured_audit_and_broadcast

    agent_metadata_model._emit_lifecycle_event(
        # A real UUID, not a placeholder string: audit.events.agent_id is clamped
        # to a joinable key or NULL, so a made-up id now correctly audits as
        # unattributed and this assertion would test the clamp, not the dedup.
        agent_id="7f3a1c02-9b45-4e18-8c6d-2a5e91d4b077",
        event="paused",
        reason="EI imbalance",
        timestamp="2026-04-23T22:00:00+00:00",
        label="Steward",
    )

    # Drain the scheduled _emit coroutine.
    assert len(scheduled) == 1
    await scheduled[0]

    broadcaster.broadcast_event.assert_awaited_once()
    call_kwargs = broadcaster.broadcast_event.await_args.kwargs
    assert call_kwargs["event_type"] == "lifecycle_paused"
    assert call_kwargs["agent_id"] == "7f3a1c02-9b45-4e18-8c6d-2a5e91d4b077"
    assert call_kwargs["payload"]["reason"] == "EI imbalance"
    assert call_kwargs["payload"]["event"] == "paused"

    # The critical assertion: no direct audit write when broadcast succeeds.
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_agent_skips_broadcast_and_still_audits(
    captured_audit_and_broadcast,
):
    """Test agents (label prefixed with test_/cli-pytest/etc) skip Discord
    but must still leave an audit trail — otherwise integration tests
    lose observability on their own agents."""
    broadcaster, audit, scheduled = captured_audit_and_broadcast

    agent_metadata_model._emit_lifecycle_event(
        agent_id="test-uuid",
        event="paused",
        reason="test pause",
        timestamp="2026-04-23T22:00:00+00:00",
        label="cli-pytest-alpha",
    )

    assert len(scheduled) == 1
    await scheduled[0]

    broadcaster.broadcast_event.assert_not_awaited()
    audit.assert_awaited_once()
    audit_args = audit.await_args.args[0]
    assert audit_args["event_type"] == "lifecycle_paused"
    assert audit_args["details"]["event"] == "paused"
    assert audit_args["details"]["reason"] == "test pause"


@pytest.mark.asyncio
async def test_broadcast_failure_falls_back_to_direct_audit(
    captured_audit_and_broadcast,
):
    """If the broadcaster errors mid-send, we must still persist the
    lifecycle event so restart-survivability is preserved."""
    broadcaster, audit, scheduled = captured_audit_and_broadcast
    broadcaster.broadcast_event.side_effect = RuntimeError("ws pipe closed")

    agent_metadata_model._emit_lifecycle_event(
        agent_id="7f3a1c02-9b45-4e18-8c6d-2a5e91d4b077",
        event="paused",
        reason="EI imbalance",
        timestamp="2026-04-23T22:00:00+00:00",
        label="Steward",
    )

    assert len(scheduled) == 1
    await scheduled[0]

    broadcaster.broadcast_event.assert_awaited_once()
    audit.assert_awaited_once()
