"""A call through use_tool is recorded as a gateway call.

use_tool re-enters the target's transport path, so the only tool_usage row the
call writes is the target's, and use_tool itself has no row. Before
2026-09-29 that row carried nothing to say it came through the gateway, so
"how often do agents reach an unlisted capability through use_tool" had no
answer: a direct call and a gateway call were the same row.
"""

import pytest

from src.services.tool_usage_recorder import build_tool_usage_payload, entered_via


def test_payload_outside_a_gateway_has_no_via():
    payload = build_tool_usage_payload("health_check", {}) or {}
    assert "via" not in payload


def test_payload_inside_a_gateway_names_it():
    with entered_via("use_tool"):
        payload = build_tool_usage_payload("health_check", {})
    assert payload == {"via": "use_tool"}
    assert "via" not in (build_tool_usage_payload("health_check", {}) or {})


def test_via_keeps_the_existing_keys():
    with entered_via("use_tool"):
        payload = build_tool_usage_payload("agent", {"action": "list"})
    assert payload["via"] == "use_tool"
    assert payload["action"] == "list"


@pytest.mark.asyncio
async def test_the_transport_path_records_the_target_with_via(monkeypatch):
    from src.mcp_handlers import context
    from src.mcp_handlers.introspection import tool_introspection

    seen = {}

    async def invoker(target, arguments):
        seen["payload"] = build_tool_usage_payload(target, arguments)
        return [{"ok": True}]

    monkeypatch.setattr(
        tool_introspection, "_registered_public_tool_names", lambda: ["health_check"]
    )
    token = context.set_nested_tool_invoker(invoker)
    try:
        await tool_introspection.handle_use_tool({"tool_name": "health_check", "arguments": {}})
    finally:
        context.reset_nested_tool_invoker(token)

    assert seen["payload"] == {"via": "use_tool"}
    assert "via" not in (build_tool_usage_payload("health_check", {}) or {})
