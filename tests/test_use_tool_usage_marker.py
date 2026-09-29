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
async def test_the_real_mcp_wrapper_writes_one_row_for_the_target_marked_via(monkeypatch):
    """Drives the registered use_tool wrapper, not a mocked invoker.

    The MCP transport's nested invoker awaits the target's own wrapper in the
    same task, and the outer use_tool wrapper skips its own row once it has
    delegated (nested_delegated in tool_registration). So one call writes one
    row: the target's, marked via=use_tool.
    """
    import src.tool_registration as tool_registration

    recorded = []
    monkeypatch.setattr(tool_registration, "_wave3a_get_route", lambda name: None)
    monkeypatch.setattr(tool_registration, "record_tool_usage", lambda **kw: recorded.append(kw))

    wrapper = tool_registration.get_tool_wrapper("use_tool")
    await wrapper(tool_name="health_check", arguments={})

    assert [row["tool_name"] for row in recorded] == ["health_check"]
    assert (recorded[0].get("payload") or {}).get("via") == "use_tool"


@pytest.mark.asyncio
async def test_a_direct_call_through_the_real_wrapper_is_unmarked(monkeypatch):
    import src.tool_registration as tool_registration

    recorded = []
    monkeypatch.setattr(tool_registration, "_wave3a_get_route", lambda name: None)
    monkeypatch.setattr(tool_registration, "record_tool_usage", lambda **kw: recorded.append(kw))

    await tool_registration.get_tool_wrapper("health_check")()

    assert [row["tool_name"] for row in recorded] == ["health_check"]
    assert "via" not in (recorded[0].get("payload") or {})
