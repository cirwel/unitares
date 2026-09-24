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



@pytest.mark.asyncio
async def test_release_presence_retires_the_sessions_bindings(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    retired = []

    async def _release(agent_uuid, session_ids=()):
        return {"released": True, "reason": "released"}

    async def _retire(agent_id):
        retired.append(agent_id)
        return 1

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "release_agent_presence", _release)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)

    body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))

    assert retired == ["caller-uuid"]
    assert body["bindings_retired"] == 1


@pytest.mark.asyncio
async def test_release_presence_keeps_bindings_while_another_session_holds(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    retired = []

    async def _release(agent_uuid, session_ids=()):
        return {"released": False, "reason": "held_by_other_session"}

    async def _retire(agent_id):
        retired.append(agent_id)
        return 1

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "release_agent_presence", _release)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)

    body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))

    assert retired == []
    assert body["bindings_retired"] == 0



@pytest.mark.asyncio
async def test_release_presence_retires_bindings_without_a_lease_plane(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    retired = []

    async def _release(agent_uuid, session_ids=()):
        return {"released": False, "reason": "lease_plane_unavailable"}

    async def _retire(agent_id):
        retired.append(agent_id)
        return 1

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "release_agent_presence", _release)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)

    body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))

    assert retired == ["caller-uuid"]
    assert body["bindings_retired"] == 1


@pytest.mark.asyncio
async def test_binding_insert_after_a_clean_exit_is_skipped(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    apl._released_at["caller-uuid"] = apl.time.monotonic()
    try:
        called = []

        def _get_db():
            called.append(True)
            raise AssertionError("must not touch the database")

        monkeypatch.setattr("src.db.get_db", _get_db)
        await process_binding.record_binding_bg("caller-uuid", object(), "sess-1")
        assert called == []
    finally:
        apl._released_at.pop("caller-uuid", None)



@pytest.mark.asyncio
async def test_release_presence_reports_a_failed_binding_retirement(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    attempts = []

    async def _release(agent_uuid, session_ids=()):
        return {"released": True, "reason": "released"}

    async def _retire(agent_id):
        attempts.append(agent_id)
        return None

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "release_agent_presence", _release)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)

    body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))

    assert len(attempts) == 2
    assert body["binding_retirement_failed"] is True
    assert body["bindings_retired"] is None


@pytest.mark.asyncio
async def test_release_presence_keeps_bindings_when_the_lease_lookup_fails(monkeypatch):
    """A failed lease lookup is not proof that no lease is live: the handler
    must not retire bindings or report a clean exit, and must say to retry."""
    from types import SimpleNamespace

    from src.mcp_handlers.identity import process_binding

    retired = []

    async def _retire(agent_id):
        retired.append(agent_id)
        return 1

    def _get_db():
        raise ConnectionError("pool unavailable")

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "_make_client", lambda: SimpleNamespace())
    monkeypatch.setattr("src.db.get_db", _get_db)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)
    apl._lease_ids.pop("caller-uuid", None)  # cold cache, as after a restart
    try:
        body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))
    finally:
        for state in (apl._released_at, apl._released_sessions, apl._lease_sessions,
                      apl._touched, apl._locks):
            state.pop("caller-uuid", None)

    assert retired == []
    assert body["released"] is False
    assert body["reason"] == "lease_lookup_failed"
    assert body["retryable"] is True
    assert body["bindings_retired"] == 0
