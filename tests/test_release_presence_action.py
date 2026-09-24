"""agent(action='release_presence') releases only the caller's own presence."""

import json

import pytest

from src.mcp_handlers.identity import agent_presence_lease as apl
from src.mcp_handlers.identity import shared
from src.mcp_handlers.lifecycle.mutation import handle_release_presence


def _payload(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_release_presence_targets_the_bound_caller_only(monkeypatch):
    released = []

    async def _release(agent_uuid, session_ids=()):
        released.append((agent_uuid, session_ids))
        return {"released": True, "reason": "released"}

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "release_agent_presence", _release)

    body = _payload(await handle_release_presence(
        {"agent_id": "someone-else", "client_session_id": "sess-1"}
    ))

    assert released == [("caller-uuid", ("sess-1",))]
    assert body["released"] is True
    assert body["agent_id"] == "caller-uuid"


@pytest.mark.asyncio
async def test_release_presence_refuses_an_unbound_caller(monkeypatch):
    called = []

    async def _release(agent_uuid, session_ids=()):
        called.append(agent_uuid)
        return {"released": True, "reason": "released"}

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: None)
    monkeypatch.setattr(apl, "release_agent_presence", _release)

    body = _payload(await handle_release_presence({}))

    assert called == []
    assert body["success"] is False


def test_checkin_presence_heartbeat_carries_the_session_id(monkeypatch):
    """sync_state's UpdateContext has no client_session_id attribute; the id in
    its arguments must still reach the presence heartbeat, or a session's own
    release would see its check-ins as an unnamed holder."""
    from types import SimpleNamespace

    from src.mcp_handlers import core

    scheduled = []
    monkeypatch.setattr(
        apl,
        "schedule_agent_presence_heartbeat",
        lambda agent_uuid, client_session_id=None: scheduled.append(
            (agent_uuid, client_session_id)
        ),
    )
    ctx = SimpleNamespace(agent_uuid="uuid-1", arguments={"client_session_id": "sess-1"})

    core._schedule_agent_presence_heartbeat(ctx)

    assert scheduled == [("uuid-1", "sess-1")]
