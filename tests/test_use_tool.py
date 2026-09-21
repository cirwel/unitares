"""The progressive gateway reaches the complete public dispatch surface."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from src.mcp_handlers.introspection.tool_introspection import handle_use_tool
from src.mcp_handlers.context import (
    SessionSignals,
    get_context_agent_id,
    get_context_client_session_id,
    get_csid_transport_injected,
    reset_csid_transport_injected,
    reset_nested_tool_invoker,
    reset_session_context,
    reset_session_signals,
    set_csid_transport_injected,
    set_nested_tool_invoker,
    set_session_context,
    set_session_signals,
)


def _payload(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_use_tool_dispatches_an_omitted_public_capability(monkeypatch):
    calls = []
    audit_rows = []

    async def fake_dispatch(name, arguments):
        calls.append((name, arguments))
        return [TextContent(type="text", text=json.dumps({"success": True, "value": 7}))]

    monkeypatch.setattr("src.mcp_handlers.dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.record_tool_usage",
        lambda **kwargs: audit_rows.append(kwargs),
    )
    result = await handle_use_tool({
        "tool_name": "health_check",
        "arguments": {"include_dependencies": False},
        "client_session_id": "session-1",
    })

    assert _payload(result) == {"success": True, "value": 7}
    assert calls == [(
        "health_check",
        {"include_dependencies": False, "client_session_id": "session-1"},
    )]
    assert audit_rows[0]["tool_name"] == "health_check"
    assert audit_rows[0]["agent_id"] is None
    assert audit_rows[0]["success"] is True
    assert audit_rows[0]["session_id"] == "session-1"
    assert "audit_only" not in audit_rows[0]


@pytest.mark.asyncio
async def test_use_tool_preserves_explicit_nested_session(monkeypatch):
    calls = []
    audit_rows = []

    async def fake_dispatch(name, arguments):
        calls.append((name, arguments))
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr("src.mcp_handlers.dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.record_tool_usage",
        lambda **kwargs: audit_rows.append(kwargs),
    )
    await handle_use_tool({
        "tool_name": "health_check",
        "arguments": {"client_session_id": "nested"},
        "client_session_id": "outer",
    })

    assert calls[0][1]["client_session_id"] == "nested"
    assert audit_rows[0]["agent_id"] is None
    assert audit_rows[0]["session_id"] == "nested"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, ""])
async def test_use_tool_preserves_explicit_empty_nested_session(
    monkeypatch, session_id
):
    calls = []

    async def fake_dispatch(name, arguments):
        calls.append((name, arguments))
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr("src.mcp_handlers.dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.record_tool_usage", lambda **_: None
    )

    await handle_use_tool({
        "tool_name": "health_check",
        "arguments": {"client_session_id": session_id},
        "client_session_id": "outer",
    })

    assert calls == [("health_check", {"client_session_id": session_id})]


@pytest.mark.asyncio
async def test_use_tool_reenters_mcp_wrapper_for_routing_and_telemetry(monkeypatch):
    dispatch_calls = []
    proxy_calls = []
    from src import tool_registration

    async def fake_dispatch(name, arguments):
        dispatch_calls.append((name, arguments))
        raise AssertionError("Python dispatch must not run after a BEAM success")

    async def fake_proxy(**kwargs):
        proxy_calls.append(kwargs)
        return SimpleNamespace(
            ok=True,
            response={"success": True, "served_by": "beam"},
            fallback_reason=None,
        )

    monkeypatch.setattr(tool_registration, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        tool_registration, "_wave3a_get_route", lambda name: "http://beam"
    )
    monkeypatch.setattr(tool_registration, "_wave3a_proxy_to_beam", fake_proxy)
    monkeypatch.setattr(tool_registration, "record_tool_usage", lambda **_: None)
    tool_registration._tool_wrappers_cache.pop("health_check", None)

    async def invoke_target(name, arguments):
        return await tool_registration._invoke_mcp_nested_tool(
            name,
            arguments,
            outer_arguments={"client_session_id": "mcp-session"},
        )

    token = set_nested_tool_invoker(invoke_target)
    try:
        result = await handle_use_tool({
            "tool_name": "health_check",
            "arguments": {"include_dependencies": False},
        })
    finally:
        reset_nested_tool_invoker(token)
        tool_registration._tool_wrappers_cache.pop("health_check", None)

    assert _payload(result) == {"success": True, "served_by": "beam"}
    assert dispatch_calls == []
    assert proxy_calls == [{
        "tool_name": "health_check",
        "beam_url": "http://beam",
        "kwargs": {
            "include_dependencies": False,
            "client_session_id": "mcp-session",
        },
    }]


@pytest.mark.asyncio
async def test_use_tool_reenters_rest_executor(monkeypatch):
    calls = []

    async def invoke_target(name, arguments):
        calls.append((name, arguments))
        return {"healthy": True}

    token = set_nested_tool_invoker(invoke_target)
    try:
        result = await handle_use_tool({
            "tool_name": "health_check",
            "arguments": {},
            "client_session_id": "rest-session",
        })
    finally:
        reset_nested_tool_invoker(token)

    assert _payload(result) == {"healthy": True}
    assert calls == [("health_check", {})]


@pytest.mark.asyncio
async def test_mcp_nested_target_keeps_transport_session_caller_proven(monkeypatch):
    from src import tool_registration

    observed = []

    async def fake_wrapper(**arguments):
        observed.append((dict(arguments), get_csid_transport_injected()))
        return {"success": True}

    monkeypatch.setattr(tool_registration, "get_tool_wrapper", lambda _name: fake_wrapper)
    monkeypatch.setattr(tool_registration, "_session_id_from_ctx", lambda _ctx: "mcp-session")

    await tool_registration._invoke_mcp_nested_tool(
        "observe",
        {},
        outer_arguments={},
    )

    # observe is not session-injected: direct MCP identity resolution consumes
    # the caller-proven mcp-session-id transport context instead.
    assert observed == [({}, False)]


@pytest.mark.asyncio
async def test_mcp_nested_target_applies_target_specific_session_injection(monkeypatch):
    from src import tool_registration

    observed = []

    async def fake_wrapper(**arguments):
        observed.append((dict(arguments), get_csid_transport_injected()))
        return {"success": True}

    monkeypatch.setattr(tool_registration, "get_tool_wrapper", lambda _name: fake_wrapper)
    monkeypatch.setattr(tool_registration, "_session_id_from_ctx", lambda _ctx: "mcp-session")

    await tool_registration._invoke_mcp_nested_tool(
        "identity",
        {},
        outer_arguments={},
    )

    assert observed == [({"client_session_id": "mcp-session"}, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, ""])
async def test_mcp_nested_target_preserves_explicit_empty_session(
    monkeypatch, session_id
):
    from src import tool_registration

    observed = []

    async def fake_wrapper(**arguments):
        observed.append((dict(arguments), get_csid_transport_injected()))
        return {"success": True}

    monkeypatch.setattr(tool_registration, "get_tool_wrapper", lambda _name: fake_wrapper)
    monkeypatch.setattr(tool_registration, "_session_id_from_ctx", lambda _ctx: "mcp-session")

    await tool_registration._invoke_mcp_nested_tool(
        "identity",
        {"client_session_id": session_id},
        outer_arguments={"client_session_id": "outer"},
    )

    assert observed == [({"client_session_id": session_id}, False)]


@pytest.mark.asyncio
async def test_rest_nested_target_rebinds_explicit_session_and_restores_outer(monkeypatch):
    from src.http_routes import access
    from src.services import http_tool_service

    seen = []

    async def fake_resolve(name, arguments, signals):
        seen.append((
            "resolve",
            name,
            dict(arguments),
            get_context_client_session_id(),
            get_csid_transport_injected(),
        ))
        from src.mcp_handlers.context import update_context_agent_id
        update_context_agent_id("00000000-0000-0000-0000-000000000002")

    async def fake_execute(name, arguments):
        seen.append((
            "execute",
            name,
            dict(arguments),
            get_context_client_session_id(),
            get_context_agent_id(),
        ))
        return {"success": True}

    monkeypatch.setattr(access, "_resolve_http_bound_agent", fake_resolve)
    monkeypatch.setattr(http_tool_service, "execute_http_tool", fake_execute)

    context_token = set_session_context(
        session_key="outer", client_session_id="outer", agent_id="outer-agent"
    )
    signals_token = set_session_signals(SessionSignals(transport="rest"))
    csid_token = set_csid_transport_injected(True)
    try:
        result = await http_tool_service.execute_nested_http_tool(
            "health_check", {"client_session_id": "nested"}
        )
        assert get_context_client_session_id() == "outer"
        assert get_context_agent_id() == "outer-agent"
        assert get_csid_transport_injected() is True
    finally:
        reset_csid_transport_injected(csid_token)
        reset_session_signals(signals_token)
        reset_session_context(context_token)

    assert result == {"success": True}
    assert seen == [
        (
            "resolve",
            "health_check",
            {"client_session_id": "nested"},
            "nested",
            False,
        ),
        (
            "execute",
            "health_check",
            {"client_session_id": "nested"},
            "nested",
            "00000000-0000-0000-0000-000000000002",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, ""])
async def test_rest_nested_target_preserves_explicit_empty_session(
    monkeypatch, session_id
):
    from src.http_routes import access
    from src.services import http_tool_service

    seen = []

    async def fake_resolve(name, arguments, signals):
        seen.append((name, dict(arguments), get_context_client_session_id()))

    async def fake_execute(name, arguments):
        seen.append((name, dict(arguments), get_context_client_session_id()))
        return {"success": True}

    monkeypatch.setattr(access, "_resolve_http_bound_agent", fake_resolve)
    monkeypatch.setattr(http_tool_service, "execute_http_tool", fake_execute)

    context_token = set_session_context(
        session_key="outer", client_session_id="outer", agent_id="outer-agent"
    )
    signals_token = set_session_signals(SessionSignals(transport="rest"))
    csid_token = set_csid_transport_injected(True)
    try:
        await http_tool_service.execute_nested_http_tool(
            "health_check", {"client_session_id": session_id}
        )
        assert get_context_client_session_id() == "outer"
        assert get_context_agent_id() == "outer-agent"
        assert get_csid_transport_injected() is True
    finally:
        reset_csid_transport_injected(csid_token)
        reset_session_signals(signals_token)
        reset_session_context(context_token)

    assert seen == [
        ("health_check", {"client_session_id": session_id}, session_id),
        ("health_check", {"client_session_id": session_id}, session_id),
    ]


@pytest.mark.asyncio
async def test_rest_nested_target_restores_outer_context_after_exception(monkeypatch):
    from src.services import http_tool_service

    async def fake_execute(name, arguments):
        raise RuntimeError("target failed")

    monkeypatch.setattr(http_tool_service, "execute_http_tool", fake_execute)

    context_token = set_session_context(
        session_key="outer", client_session_id="outer", agent_id="outer-agent"
    )
    csid_token = set_csid_transport_injected(True)
    try:
        with pytest.raises(RuntimeError, match="target failed"):
            await http_tool_service.execute_nested_http_tool(
                "health_check", {"client_session_id": "nested"}
            )
        assert get_context_client_session_id() == "outer"
        assert get_context_agent_id() == "outer-agent"
        assert get_csid_transport_injected() is True
    finally:
        reset_csid_transport_injected(csid_token)
        reset_session_context(context_token)


@pytest.mark.asyncio
async def test_stdio_gateway_reenters_call_boundary_with_request_side_actor(monkeypatch):
    import src.mcp_handlers as handlers
    from src import mcp_server_std as stdio

    audit_rows = []

    async def fake_dispatch(name, arguments):
        if name == "use_tool":
            return await handle_use_tool(arguments)
        assert name == "health_check"
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr(handlers, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(stdio, "record_tool_usage", lambda **kw: audit_rows.append(kw))
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)
    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", None)

    result = await stdio.call_tool("use_tool", {
        "tool_name": "health_check",
        "arguments": {},
        "client_session_id": "stdio-session",
    })

    assert _payload(result) == {"success": True}
    target_row = next(row for row in audit_rows if row["tool_name"] == "health_check")
    assert target_row["agent_id"] is None
    assert target_row["session_id"] == "stdio-session"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, ""])
async def test_stdio_gateway_preserves_explicit_empty_target_session(
    monkeypatch, session_id
):
    import src.mcp_handlers as handlers
    from src import mcp_server_std as stdio

    calls = []

    async def fake_dispatch(name, arguments):
        if name == "use_tool":
            return await handle_use_tool(arguments)
        calls.append((name, dict(arguments)))
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr(handlers, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(stdio, "record_tool_usage", lambda **_: None)
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)
    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", None)

    result = await stdio.call_tool("use_tool", {
        "tool_name": "health_check",
        "arguments": {"client_session_id": session_id},
        "client_session_id": "outer-session",
    })

    assert _payload(result) == {"success": True}
    assert calls == [("health_check", {"client_session_id": session_id})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error_code"),
    [
        ({"tool_name": "use_tool", "arguments": {}}, "RECURSIVE_TOOL_INVOCATION"),
        ({"tool_name": "health_check", "arguments": []}, "INVALID_TOOL_ARGUMENTS"),
        ({"tool_name": "not_a_public_tool", "arguments": {}}, "TOOL_NOT_FOUND"),
    ],
)
async def test_use_tool_refuses_invalid_targets_and_arguments(arguments, error_code):
    payload = _payload(await handle_use_tool(arguments))
    assert payload["success"] is False
    assert payload["error_code"] == error_code


@pytest.mark.asyncio
async def test_use_tool_accepts_late_registered_public_capability(monkeypatch):
    from src import mcp_handlers
    from src.interface_contract import get_interface_contract_summary
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, mcp_tool
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    calls = []
    tool_name = "late_plugin_tool"
    before_contract = get_interface_contract_summary()

    @mcp_tool(tool_name, requires_identity="pre_onboard")
    async def late_handler(arguments):
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    async def fake_dispatch(name, arguments):
        calls.append((name, arguments))
        return await late_handler(arguments)

    monkeypatch.setattr(mcp_handlers, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.record_tool_usage", lambda **_: None
    )
    try:
        listed = _payload(await handle_list_tools({"lite": True}))
        listed_names = {tool["name"] for tool in listed["tools"]}
        result = await handle_use_tool({
            "tool_name": tool_name,
            "arguments": {"value": 7},
        })

        assert tool_name in listed_names
        assert listed["interface_contract"]["capability_count"] == len(listed_names)
        assert (
            listed["interface_contract"]["surface_sha256"]
            != before_contract["surface_sha256"]
        )
        assert _payload(result) == {"success": True}
        assert calls == [(tool_name, {"value": 7})]
    finally:
        mcp_handlers.TOOL_HANDLERS.pop(tool_name, None)
        _TOOL_DEFINITIONS.pop(tool_name, None)


@pytest.mark.asyncio
async def test_use_tool_rejects_late_registered_hidden_capability(monkeypatch):
    from src import mcp_handlers
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, mcp_tool
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    tool_name = "late_hidden_plugin_tool"

    @mcp_tool(tool_name, hidden=True, requires_identity="pre_onboard")
    async def hidden_handler(arguments):
        raise AssertionError("hidden handler must not be gateway-dispatchable")

    try:
        listed = _payload(await handle_list_tools({"lite": True}))
        listed_names = {tool["name"] for tool in listed["tools"]}
        result = _payload(await handle_use_tool({
            "tool_name": tool_name,
            "arguments": {},
        }))

        assert tool_name not in listed_names
        assert result["error_code"] == "TOOL_NOT_FOUND"
    finally:
        mcp_handlers.TOOL_HANDLERS.pop(tool_name, None)
        _TOOL_DEFINITIONS.pop(tool_name, None)


def test_use_tool_is_directly_advertised_but_hidden_targets_are_not():
    from src.interface_contract import get_public_tool_definitions

    progressive = {tool.name for tool in get_public_tool_definitions("progressive")}
    full = {tool.name for tool in get_public_tool_definitions("full")}

    assert "use_tool" in progressive
    assert "health_check" not in progressive
    assert "health_check" in full


def test_use_tool_does_not_replace_target_specific_session_policy():
    from src.tool_registration import TOOLS_NEEDING_SESSION_INJECTION

    assert not TOOLS_NEEDING_SESSION_INJECTION.matches("use_tool")
