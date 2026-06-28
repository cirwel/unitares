"""Presence refresh for successful off-path agent activity."""

from unittest.mock import AsyncMock, MagicMock

from src.services import tool_usage_recorder as recorder


AGENT_UUID = "7750bf80-20ad-4108-a952-5271b73845b8"


def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


def _patch_recorder_io(monkeypatch):
    tracker = MagicMock()
    append = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.tool_usage_tracker.get_tool_usage_tracker",
        lambda: tracker,
    )
    monkeypatch.setattr(
        "src.audit_db.append_tool_usage_async",
        append,
    )
    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task",
        _consume_coro,
    )
    return tracker, append


def test_successful_outcome_event_refreshes_presence(monkeypatch):
    _tracker, append = _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(
            (agent_id, client_session_id)
        ),
    )

    recorder.record_tool_usage(
        tool_name="outcome_event",
        agent_id=AGENT_UUID,
        success=True,
        session_id="sess-1",
    )

    assert scheduled == [(AGENT_UUID, "sess-1")]
    assert append.call_args.kwargs["session_id"] == "sess-1"


def test_presence_refresh_skips_failures_labels_and_non_activity(monkeypatch):
    _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(
            (agent_id, client_session_id)
        ),
    )

    recorder.record_tool_usage("outcome_event", AGENT_UUID, success=False)
    recorder.record_tool_usage("outcome_event", "display-name", success=True)
    recorder.record_tool_usage("health_check", AGENT_UUID, success=True)

    assert scheduled == []
