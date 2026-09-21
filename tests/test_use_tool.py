"""The progressive gateway reaches the complete public dispatch surface."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from src.mcp_handlers.introspection.tool_introspection import handle_use_tool
from src.mcp_handlers.context import (
    reset_tool_dispatch_surface,
    set_tool_dispatch_surface,
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
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.resolve_dispatch_bound_agent_id",
        lambda _arguments: "00000000-0000-0000-0000-000000000001",
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
    assert audit_rows[0]["agent_id"] == "00000000-0000-0000-0000-000000000001"
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
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.resolve_dispatch_bound_agent_id",
        lambda arguments: (
            "00000000-0000-0000-0000-000000000002"
            if arguments.get("client_session_id") == "nested"
            else None
        ),
    )

    await handle_use_tool({
        "tool_name": "health_check",
        "arguments": {"client_session_id": "nested"},
        "client_session_id": "outer",
    })

    assert calls[0][1]["client_session_id"] == "nested"
    assert audit_rows[0]["agent_id"] == "00000000-0000-0000-0000-000000000002"
    assert audit_rows[0]["session_id"] == "nested"


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
    token = set_tool_dispatch_surface("mcp")
    try:
        result = await handle_use_tool({
            "tool_name": "health_check",
            "arguments": {"include_dependencies": False},
            "client_session_id": "mcp-session",
        })
    finally:
        reset_tool_dispatch_surface(token)
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

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"healthy": True}

    monkeypatch.setattr(
        "src.services.http_tool_service.execute_http_tool", fake_execute
    )
    token = set_tool_dispatch_surface("rest")
    try:
        result = await handle_use_tool({
            "tool_name": "health_check",
            "arguments": {},
            "client_session_id": "rest-session",
        })
    finally:
        reset_tool_dispatch_surface(token)

    assert _payload(result) == {"healthy": True}
    assert calls == [("health_check", {"client_session_id": "rest-session"})]


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


def test_use_tool_is_directly_advertised_but_hidden_targets_are_not():
    from src.interface_contract import get_public_tool_definitions

    progressive = {tool.name for tool in get_public_tool_definitions("progressive")}
    full = {tool.name for tool in get_public_tool_definitions("full")}

    assert "use_tool" in progressive
    assert "health_check" not in progressive
    assert "health_check" in full


def test_use_tool_receives_transport_session_injection():
    from src.tool_registration import TOOLS_NEEDING_SESSION_INJECTION

    assert TOOLS_NEEDING_SESSION_INJECTION.matches("use_tool")
