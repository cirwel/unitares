"""Resident-pause escalation.

A configured resident stuck in a paused (circuit-breaker) state must escalate to
the operator. The silence detector only inspects status=="active" agents, so a
paused resident is otherwise unmonitored — that is how a paused Sentinel stayed
dark to governance for ~18h on 2026-06-13 with no operator-facing alert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src import background_tasks
from src.agent_metadata_model import AgentMetadata, agent_metadata


@pytest.fixture
def isolated_pause_state(monkeypatch):
    agent_metadata.clear()
    background_tasks._resident_pause_alerted.clear()

    broadcaster = AsyncMock()
    audit = AsyncMock()
    import src.broadcaster as broadcaster_module
    import src.audit_db as audit_module

    monkeypatch.setattr(broadcaster_module, "broadcaster_instance", broadcaster)
    monkeypatch.setattr(audit_module, "append_audit_event_async", audit)

    yield broadcaster, audit

    agent_metadata.clear()
    background_tasks._resident_pause_alerted.clear()


def _paused_resident(agent_id: str, label: str, paused_minutes_ago: int = 30) -> AgentMetadata:
    now_iso = datetime.now(timezone.utc).isoformat()
    paused_at = (datetime.now(timezone.utc) - timedelta(minutes=paused_minutes_ago)).isoformat()
    return AgentMetadata(
        agent_id=agent_id,
        status="paused",
        created_at=now_iso,
        last_update=paused_at,
        label=label,
        paused_at=paused_at,
    )


@pytest.mark.asyncio
async def test_paused_resident_escalates_critical(isolated_pause_state):
    broadcaster, audit = isolated_pause_state
    agent_metadata["sentinel-uuid"] = _paused_resident("sentinel-uuid", "Sentinel")

    await background_tasks._resident_pause_check_iteration()

    broadcaster.broadcast_event.assert_awaited_once()
    call = broadcaster.broadcast_event.await_args
    assert call.args[0] == "lifecycle_resident_paused"
    assert call.kwargs["agent_id"] == "sentinel-uuid"
    # severity=critical is what the bridge routes to #alerts
    assert call.kwargs["payload"]["severity"] == "critical"
    assert call.kwargs["payload"]["label"] == "Sentinel"
    assert call.kwargs["payload"]["paused_minutes"] is not None
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_paused_ephemeral_agent_not_escalated(isolated_pause_state):
    # Routine ephemeral-agent pauses must NOT page the operator.
    broadcaster, _ = isolated_pause_state
    agent_metadata["eph-uuid"] = _paused_resident("eph-uuid", "claude_code-claude_abcd")

    await background_tasks._resident_pause_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_resident_not_escalated(isolated_pause_state):
    broadcaster, _ = isolated_pause_state
    now_iso = datetime.now(timezone.utc).isoformat()
    agent_metadata["sentinel-uuid"] = AgentMetadata(
        agent_id="sentinel-uuid",
        status="active",
        created_at=now_iso,
        last_update=now_iso,
        label="Sentinel",
    )

    await background_tasks._resident_pause_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_respam_within_renag_window(isolated_pause_state):
    broadcaster, _ = isolated_pause_state
    agent_metadata["sentinel-uuid"] = _paused_resident("sentinel-uuid", "Sentinel")

    await background_tasks._resident_pause_check_iteration()
    await background_tasks._resident_pause_check_iteration()  # immediate second pass

    broadcaster.broadcast_event.assert_awaited_once()  # throttled within re-nag window


@pytest.mark.asyncio
async def test_rearm_after_resume(isolated_pause_state):
    broadcaster, _ = isolated_pause_state
    meta = _paused_resident("sentinel-uuid", "Sentinel")
    agent_metadata["sentinel-uuid"] = meta

    await background_tasks._resident_pause_check_iteration()
    assert "sentinel-uuid" in background_tasks._resident_pause_alerted

    # Resident resumes → watchdog re-arms (forgets it).
    meta.status = "active"
    meta.paused_at = None
    await background_tasks._resident_pause_check_iteration()
    assert "sentinel-uuid" not in background_tasks._resident_pause_alerted

    # Pauses again → escalates immediately (not throttled).
    meta.status = "paused"
    meta.paused_at = datetime.now(timezone.utc).isoformat()
    await background_tasks._resident_pause_check_iteration()
    assert broadcaster.broadcast_event.await_count == 2
