"""agent(action='release_presence') releases only the caller's own presence."""

import json

import pytest

from src.mcp_handlers.identity import agent_presence_lease as apl
from src.mcp_handlers.identity import shared
from src.mcp_handlers.lifecycle.mutation import handle_release_presence


@pytest.fixture(autouse=True)
def _clear_presence_state():
    """The handler records exit markers in module state; keep tests isolated."""
    yield
    for state in (apl._bindings_retired_at, apl._binding_inserts_in_flight,
                  apl._exited_during_insert):
        state.clear()


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

    apl.mark_bindings_retired("caller-uuid")
    try:
        called = []

        def _get_db():
            called.append(True)
            raise AssertionError("must not touch the database")

        monkeypatch.setattr("src.db.get_db", _get_db)
        await process_binding.record_binding_bg("caller-uuid", object(), "sess-1")
        assert called == []
    finally:
        apl._bindings_retired_at.pop("caller-uuid", None)



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


@pytest.mark.asyncio
async def test_release_presence_retires_bindings_when_the_lease_is_already_gone(monkeypatch):
    """A cached lease the lease plane reports not_found (force-released) is no
    live presence; the exiting session's bindings must be retired."""
    from types import SimpleNamespace

    from src.mcp_handlers.identity import process_binding

    retired = []

    async def _retire(agent_id):
        retired.append(agent_id)
        return 1

    fake = SimpleNamespace(
        release=lambda req, identity_proof=None: SimpleNamespace(ok=False, error="not_found")
    )
    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "_make_client", lambda: fake)
    monkeypatch.setattr(apl, "ReleaseRequest", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(apl, "_mint_presence_attestation", lambda *a, **k: None)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)
    apl._lease_ids["caller-uuid"] = "lease-gone"
    try:
        body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))
    finally:
        for state in (apl._lease_ids, apl._released_at, apl._released_sessions,
                      apl._lease_sessions, apl._touched, apl._locks):
            state.pop("caller-uuid", None)

    assert body["reason"] == "no_live_lease"
    assert retired == ["caller-uuid"]
    assert "retryable" not in body


def _stalled_insert_harness(monkeypatch, release_result):
    """A binding insert that stalls on the database; while it waits, the
    identity calls release_presence (stubbed to ``release_result``) and then
    more than the suppression window passes before the insert returns."""
    from types import SimpleNamespace

    from src.mcp_handlers.identity import process_binding

    clock = [1000.0]
    monkeypatch.setattr(apl.time, "monotonic", lambda: clock[0])
    statements = []

    async def _release(agent_uuid, session_ids=()):
        return dict(release_result)

    async def _retire(agent_id):
        return 1

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    if release_result is None:
        # Drive the real release: another session still holds the lease.
        monkeypatch.setattr(apl, "_make_client", lambda: SimpleNamespace())
        apl._lease_sessions["caller-uuid"] = {"sess-other": clock[0]}
    else:
        monkeypatch.setattr(apl, "release_agent_presence", _release)
    monkeypatch.setattr(process_binding, "retire_bindings", _retire)

    class _Conn:
        async def execute(self, sql, *args):
            statements.append(sql.split()[0])
            if sql.split()[0] == "INSERT":
                await handle_release_presence({"client_session_id": "sess-1"})
                clock[0] += apl._RELEASE_SUPPRESS_S + 60
            return "UPDATE 1"

        async def fetch(self, *a):
            return []

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("src.db.get_db", lambda: SimpleNamespace(acquire=lambda: _Acquire()))
    fp = SimpleNamespace(host_id="h", pid=1, pid_start_time=1.0, transport="stdio",
                         ppid=None, tty=None, anchor_path_hash=None)

    async def run():
        try:
            await process_binding.record_binding_bg("caller-uuid", fp, "sess-1")
        finally:
            for state in (apl._bindings_retired_at, apl._released_at, apl._released_sessions,
                          apl._lease_sessions, apl._touched, apl._locks, apl._lease_ids):
                state.pop("caller-uuid", None)
        return statements

    return run


@pytest.mark.asyncio
async def test_binding_insert_stalled_past_the_suppression_window_is_retired(monkeypatch):
    """The identity exits while its insert waits on the database, and the insert
    lands only after the suppression window. The row it wrote must still be
    retired, or the exited parent reads live."""
    run = _stalled_insert_harness(monkeypatch, {"released": True, "reason": "released"})

    assert await run() == ["INSERT", "UPDATE"]
    assert "caller-uuid" not in apl._binding_inserts_in_flight
    assert "caller-uuid" not in apl._exited_during_insert


@pytest.mark.asyncio
async def test_release_that_leaves_another_holder_keeps_an_in_flight_binding(monkeypatch):
    """A release that left the lease to another live session retires no
    bindings, so a binding insert in flight keeps its row."""
    run = _stalled_insert_harness(monkeypatch, None)

    assert await run() == ["INSERT"]
    assert "caller-uuid" not in apl._exited_during_insert


@pytest.mark.asyncio
async def test_release_that_leaves_another_holder_does_not_skip_later_inserts(monkeypatch):
    from src.mcp_handlers.identity import process_binding

    from types import SimpleNamespace

    monkeypatch.setattr(shared, "require_write_permission", lambda arguments=None: (True, None))
    monkeypatch.setattr(shared, "get_bound_agent_id", lambda session_id=None, arguments=None: "caller-uuid")
    monkeypatch.setattr(apl, "_make_client", lambda: SimpleNamespace())
    apl._lease_sessions["caller-uuid"] = {"sess-other": apl.time.monotonic()}
    try:
        body = _payload(await handle_release_presence({"client_session_id": "sess-1"}))
    finally:
        for state in (apl._released_at, apl._released_sessions, apl._lease_sessions,
                      apl._touched, apl._locks):
            state.pop("caller-uuid", None)

    assert body["reason"] == "held_by_other_session"
    assert process_binding._recently_exited("caller-uuid") is False
