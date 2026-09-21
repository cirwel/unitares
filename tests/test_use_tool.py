"""The progressive gateway reaches the complete public dispatch surface."""

from __future__ import annotations

import json

import pytest
from mcp.types import TextContent

from src.mcp_handlers.introspection.tool_introspection import handle_use_tool


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
        "src.services.tool_usage_recorder.resolve_audit_agent_id",
        lambda _value: "agent-1",
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
    assert audit_rows[0]["success"] is True
    assert audit_rows[0]["session_id"] == "session-1"
    assert audit_rows[0]["audit_only"] is True


@pytest.mark.asyncio
async def test_use_tool_preserves_explicit_nested_session(monkeypatch):
    calls = []

    async def fake_dispatch(name, arguments):
        calls.append((name, arguments))
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr("src.mcp_handlers.dispatch_tool", fake_dispatch)
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.record_tool_usage", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "src.services.tool_usage_recorder.resolve_audit_agent_id", lambda _value: None
    )

    await handle_use_tool({
        "tool_name": "health_check",
        "arguments": {"client_session_id": "nested"},
        "client_session_id": "outer",
    })

    assert calls[0][1]["client_session_id"] == "nested"


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

