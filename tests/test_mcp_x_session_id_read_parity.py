"""/mcp/ honours an X-Session-ID header on reads the way it does on writes.

A caller that sent only ``X-Session-ID: <its own client_session_id>`` got its
own state back from REST ``check_working_state`` and an 'unbound' payload
from the same call on ``/mcp/`` (verified live 2026-09-26). The /mcp/ write
path already accepted the header: ``derive_session_key`` keys a header-only
call on it at step 4 and marks it ``caller_asserted``. The read was dropped
before resolution by the #945 section 1 short-circuit, which counted only
argument-level proof.

The obvious suspect, the typed wrapper's transport injection, is not the
cause: on a direct /mcp/ call it never runs, because FastMCP None-fills every
declared argument before the wrapper sees it. ``tests/test_wrapper_generator.py``
calls the wrapper directly and so cannot see that; the first two tests here go
through the registered FastMCP tool instead.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from src.mcp_handlers.context import SessionSignals

AGENT_UUID = "1856bb5c-2553-4523-809b-a5d26bbd58d1"
HEADER = "agent-1856bb5c-255"
FINGERPRINT = "127.0.0.1:abc123"

SESSION_TOOLS = ["check_working_state", "get_governance_metrics", "sync_state", "process_agent_update"]
READS = ["check_working_state", "get_governance_metrics"]
WRITES = ["sync_state", "process_agent_update"]


def _signals(**overrides) -> SessionSignals:
    fields = {
        "x_session_id": HEADER,
        "ip_ua_fingerprint": FINGERPRINT,
        "user_agent": "curl/8.7.1",
        "transport": "mcp",
    }
    fields.update(overrides)
    return SessionSignals(**fields)


# --- the wire: FastMCP validation, then the typed wrapper -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", SESSION_TOOLS)
async def test_direct_mcp_call_never_injects_the_transport_session(monkeypatch, tool_name):
    """Through the registered tool, an omitted client_session_id reaches
    dispatch absent and unflagged, although the extractor has a value.

    If a change ever makes this inject, the header would be flagged
    transport-injected and a header-only write would derive server_inferred,
    which STRICT_IDENTITY_REQUIRED refuses. Fix that deliberately, not by
    accident.
    """
    import src.tool_registration as registration
    from src import mcp_server
    from src.mcp_handlers.context import (
        get_csid_transport_injected,
        reset_session_signals,
        set_session_signals,
    )

    delivered: dict = {}

    async def spy(name, kwargs):
        delivered["arguments"] = dict(kwargs)
        delivered["injected"] = get_csid_transport_injected()
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr(registration, "dispatch_tool", spy)
    monkeypatch.setattr(registration, "record_tool_usage", lambda **kwargs: None)

    tool = mcp_server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None and tool.context_kwarg == "ctx"
    # The None-fill that keeps the wrapper's `not in kwargs` test false.
    assert tool.fn_metadata.validate_arguments({})["client_session_id"] is None

    token = set_session_signals(_signals())
    try:
        # The extractor WOULD inject the header if the wrapper asked it.
        assert registration._session_id_from_ctx(None) == HEADER
        # A non-None context, so the wrapper's `if session_extractor and ctx`
        # guard is passed and only the None-fill stops the injection.
        await tool.run({}, context=SimpleNamespace(), convert_result=False)
    finally:
        reset_session_signals(token)

    assert "client_session_id" not in delivered["arguments"]
    assert delivered["injected"] is False


@pytest.mark.asyncio
async def test_wrapper_called_directly_does_inject():
    """Control for the test above: the same wrapper without FastMCP in front
    does inject, which is all the direct-wrapper tests can observe."""
    from src.mcp_handlers.support.wrapper_generator import create_typed_wrapper

    seen: dict = {}

    def get_handler(name):
        async def handler(**kwargs):
            seen.update(kwargs)
            return {}
        return handler

    wrapper = create_typed_wrapper(
        "session_tool",
        {"properties": {"client_session_id": {"type": "string"}}, "required": []},
        get_handler,
        inject_session=True,
        session_extractor=lambda ctx: HEADER,
    )
    await wrapper(ctx=SimpleNamespace())
    assert seen == {"client_session_id": HEADER}


# --- the middleware: which calls resolve, and on which key ------------------


def _no_consult():
    return SimpleNamespace(binding=None, cacheable=False, transport_key=None)


async def _resolve(tool_name: str, signals: SessionSignals, resolve_result: dict):
    """The real identity step with the real derive_session_key; only storage
    (session lookup, sticky cache, pin, anchor, TTL) is stubbed."""
    from src.mcp_handlers.context import (
        get_session_proof_origin,
        get_session_resolution_source,
        reset_session_signals,
        set_session_signals,
    )
    from src.mcp_handlers.middleware import DispatchContext, resolve_identity

    resolve = AsyncMock(return_value=resolve_result)
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    token = set_session_signals(signals)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step.consult_sticky_binding",
                AsyncMock(return_value=_no_consult()),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.session.lookup_onboard_pin",
                AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._anchor_resolved_identity",
            ))
            stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
            ctx = DispatchContext()
            await resolve_identity(tool_name, {}, ctx)
            observed = {
                "ctx": ctx,
                "resolve": resolve,
                "session_key": ctx.session_key,
                "proof_origin": get_session_proof_origin(),
                "source": get_session_resolution_source(),
            }
    finally:
        reset_session_signals(token)
    return observed


def _hit():
    return {"agent_uuid": AGENT_UUID, "created": False, "persisted": False, "source": "session_binding"}


def _miss():
    return {"resume_failed": True, "error": "session_resolve_miss", "session_key": HEADER}


@pytest.mark.asyncio
@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("write", WRITES)
async def test_header_only_read_resolves_on_the_key_a_header_only_write_uses(read, write):
    read_seen = await _resolve(read, _signals(), _hit())
    write_seen = await _resolve(write, _signals(), _hit())

    assert read_seen["resolve"].await_count == 1, "the read was short-circuited"
    assert read_seen["session_key"] == write_seen["session_key"] == HEADER
    assert read_seen["resolve"].await_args.args[0] == HEADER
    for seen in (read_seen, write_seen):
        assert seen["source"] == "x_session_id"
        assert seen["proof_origin"] == "caller_asserted"
    assert read_seen["ctx"].bound_agent_id == AGENT_UUID


@pytest.mark.asyncio
async def test_header_only_read_returns_the_callers_own_state():
    """End of the read: the real metrics handler, in the context the real
    identity step left, serves the bound agent rather than unbound."""
    import src.mcp_handlers.core as core_mod
    from src.mcp_handlers.context import reset_session_signals, set_session_signals
    from src.mcp_handlers.middleware import DispatchContext, resolve_identity

    data = AsyncMock(return_value={"ok": True, "agent_id": AGENT_UUID})
    token = set_session_signals(_signals())
    try:
        with patch(
            "src.mcp_handlers.middleware.identity_step.consult_sticky_binding",
            AsyncMock(return_value=_no_consult()),
        ), patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            AsyncMock(return_value=_hit()),
        ), patch(
            "src.mcp_handlers.middleware.identity_step._anchor_resolved_identity",
        ), patch.object(
            core_mod.mcp_server, "agent_metadata", {AGENT_UUID: SimpleNamespace(label="a")},
        ), patch(
            "src.services.runtime_queries.get_governance_metrics_data", data,
        ):
            await resolve_identity("check_working_state", {}, DispatchContext())
            result = json.loads((await core_mod.handle_get_governance_metrics({}))[0].text)
    finally:
        reset_session_signals(token)

    assert result.get("ok") is True, result
    assert data.await_args.args[0] == AGENT_UUID


@pytest.mark.asyncio
@pytest.mark.parametrize("read", READS)
async def test_header_naming_an_unknown_session_stays_unbound(read):
    import src.mcp_handlers.core as core_mod

    seen = await _resolve(read, _signals(), _miss())

    assert seen["resolve"].await_count == 1
    assert seen["ctx"].bound_agent_id is None
    # No middleware auto-mint for a pre_onboard read.
    assert all(not call.kwargs.get("force_new") for call in seen["resolve"].await_args_list)
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
        payload = json.loads((await core_mod.handle_get_governance_metrics({}))[0].text)
    assert payload["status"] == "⚪ unbound"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signals",
    [
        pytest.param(_signals(x_session_id=None), id="fingerprint-only"),
        # Mcp-Session-Id outranks the header at derive step 3 and is
        # connection-scoped (subagents share the parent's connection).
        pytest.param(_signals(mcp_session_id="conn-1"), id="mcp-session-id-wins"),
        pytest.param(_signals(x_session_id=None, x_client_id="client-1"), id="x-client-id"),
        pytest.param(_signals(x_session_id=None, oauth_client_id="oauth-1"), id="oauth-client-id"),
    ],
)
@pytest.mark.parametrize("read", READS)
async def test_other_transport_signals_are_not_read_proof(read, signals):
    seen = await _resolve(read, signals, _hit())

    assert seen["resolve"].await_count == 0, f"{read} resolved on {seen['source']}"
    assert seen["ctx"].bound_agent_id is None
