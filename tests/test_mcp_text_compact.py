"""/mcp/ sends a tool's JSON as compact text, not FastMCP's indented rendering.

``get_tool_wrapper`` parses the handler's JSON text into a dict. Handed that
dict, FastMCP renders it itself with
``pydantic_core.to_json(result, fallback=str, indent=2)`` (``_convert_to_content``
in the SDK's ``func_metadata``, on both majors), so until 2026-09-28 every
/mcp/ result reached an agent's context indented. Over the 14 days to that
date the indentation was 13.5% of Claude Code governance tool-result bytes.
The /mcp/ registrars now hand FastMCP one compact text block
(``src.tool_registration._mcp_wire_result``).

Each call here goes through a real MCP client over the SDK's in-memory
transport into the real registered server (``src.mcp_server.mcp``): the
typed wrapper, the tool wrapper, and FastMCP's result conversion. Only
``dispatch_tool`` is stubbed, so the handler text is known exactly.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pydantic_core
import pytest
from mcp.types import TextContent

from src.mcp_compat import MCP_MAJOR, lowlevel_server

# Nested objects, arrays of scalars (FastMCP's indent put each element on its
# own line), non-ASCII, an escaped newline inside a string, floats and null.
PAYLOAD = {
    "success": True,
    "state_summary": {"action": "proceed", "coherence": 0.51, "risk_score": 0.12},
    "E": 0.7,
    "I": 0.64,
    "S": 0.2,
    "V": -0.03,
    "recent": [0.1, 0.2, 0.30000000000000004],
    "note": "héllo — two\nlines",
    "nothing": None,
    "nested": [{"id": "d-1", "tags": ["a", "b"]}, {"id": "d-2", "tags": []}],
}

# A simple typed wrapper, a session-injection wrapper, and a workflow alias:
# every /mcp/ registration path.
TOOLS = ["list_tools", "identity", "check_working_state"]


@pytest.fixture
def handler(monkeypatch):
    """Stub dispatch; the test sets what the handler returns."""
    import src.tool_registration as registration

    box: dict = {}

    async def dispatch(name, arguments):
        box.setdefault("calls", []).append(name)
        if "raise" in box:
            raise box["raise"]
        return [TextContent(type="text", text=box["text"])]

    monkeypatch.setattr(registration, "dispatch_tool", dispatch)
    monkeypatch.setattr(registration, "record_tool_usage", lambda **_: None)
    monkeypatch.setattr(registration, "_wave3a_get_route", lambda _name: None)
    registration._tool_wrappers_cache.clear()
    yield box
    registration._tool_wrappers_cache.clear()


async def _over_mcp(method: str, *args):
    """Call ``method`` on a real MCP client session against the real server."""
    from src import mcp_server

    if MCP_MAJOR == 2:
        from mcp.client import Client

        async with Client(mcp_server.mcp) as client:
            return await getattr(client, method)(*args)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        lowlevel_server(mcp_server.mcp)
    ) as session:
        return await getattr(session, method)(*args)


def _wire(result) -> dict:
    """A result as it is serialized onto the wire."""
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _call(tool_name: str, arguments: dict | None = None) -> dict:
    """One tools/call, as the client receives it."""
    return _wire(await _over_mcp("call_tool", tool_name, arguments or {}))


def _text(wire: dict) -> str:
    assert wire.get("isError") is False, wire
    blocks = wire["content"]
    assert [block["type"] for block in blocks] == ["text"], blocks
    return blocks[0]["text"]


def _without_insignificant_whitespace(text: str) -> str:
    """JSON text with every whitespace character outside strings removed."""
    out, in_string, escaped = [], False, False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif char not in " \t\n\r":
            out.append(char)
    return "".join(out)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", TOOLS)
async def test_the_text_is_compact_and_carries_the_handlers_json(handler, tool_name):
    handler["text"] = json.dumps(PAYLOAD, ensure_ascii=False)

    wire = await _call(tool_name)
    text = _text(wire)

    assert handler["calls"] == [tool_name]
    assert "\n  " not in text
    assert "\n" not in text, text
    assert len(text) <= len(handler["text"])
    assert json.loads(text) == json.loads(handler["text"])
    # Exactly what FastMCP sent before, minus whitespace outside strings.
    before = pydantic_core.to_json(
        json.loads(handler["text"]), fallback=str, indent=2
    ).decode()
    assert "\n  " in before, "premise: FastMCP's own rendering is indented"
    assert text == _without_insignificant_whitespace(before)
    # Nothing but the text block is on the wire, before or after.
    assert "structuredContent" not in wire


def test_no_registered_tool_declares_an_output_schema():
    """The premise that makes the text block everything a client reads: with
    ``structured_output=False`` no tool has an outputSchema, so FastMCP emits
    no structuredContent for any of them. If this fails, a client may read
    structuredContent, and ``_mcp_wire_result`` returning a bare text block
    would drop it: revisit that function before relaxing this."""
    from src import mcp_server

    tools = mcp_server.mcp._tool_manager.list_tools()
    assert len(tools) > 40
    declared = [t.name for t in tools if t.fn_metadata.output_schema is not None]
    assert declared == []


@pytest.mark.asyncio
async def test_the_listing_advertises_no_output_schema():
    tools = _wire(await _over_mcp("list_tools"))["tools"]
    assert tools
    assert [t["name"] for t in tools if "outputSchema" in t] == []


@pytest.mark.asyncio
async def test_non_json_handler_text_keeps_its_fallback_envelope(handler):
    handler["text"] = "plain words, not JSON\nsecond line"

    text = _text(await _call("list_tools"))

    assert json.loads(text) == {"success": True, "text": handler["text"]}
    assert "\n" not in text


@pytest.mark.asyncio
async def test_a_wrapper_error_envelope_is_compact_too(handler):
    handler["raise"] = RuntimeError("dispatch blew up")

    text = _text(await _call("list_tools"))

    assert json.loads(text) == {
        "success": False,
        "error": "dispatch blew up",
        "error_type": "RuntimeError",
    }
    assert "\n" not in text


# --- the SDK still reads the verdict and the reading off the compact text ---

_CHECKIN = {
    "proceed": {
        "decision": {"action": "proceed", "sub_action": "approve", "reason": "Low risk (0.12)"},
        "metrics": {"coherence": 0.82, "risk_score": 0.12, "verdict": "safe"},
    },
    "pause": {
        "decision": {"action": "pause", "sub_action": "risk_pause", "reason": "High risk (0.91)"},
        "metrics": {"coherence": 0.41, "risk_score": 0.91, "verdict": "unsafe"},
    },
}


def _compact_checkin_envelope(shape: str) -> dict:
    """What a default (compact) sync_state caller receives, built by the
    server's own formatter and envelope builder."""
    from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
    from src.mcp_handlers.response_formatter import format_response

    source = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        **deepcopy(_CHECKIN[shape]),
    }
    formatted = format_response(source, {"response_mode": "compact"}, task_type="feature")
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "compact"}
    )
    assert "decision" not in env and "metrics" not in env, "premise: enveloped shape"
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_CHECKIN))
async def test_the_sdk_resolves_a_checkin_from_the_compact_text(handler, shape):
    from unitares_sdk._checkin_fields import resolve_checkin_fields
    from unitares_sdk.client import GovernanceClient

    env = _compact_checkin_envelope(shape)
    handler["text"] = json.dumps(env, ensure_ascii=False)

    result = await _over_mcp("call_tool", "sync_state", {"response_text": "x"})
    assert "\n" not in _text(_wire(result))

    fields = resolve_checkin_fields(GovernanceClient._parse_mcp_result(result))

    canonical = _CHECKIN[shape]
    assert fields["verdict"] == env["state_summary"]["action"]
    assert fields["verdict"] == canonical["decision"]["action"]
    assert fields["coherence"] == canonical["metrics"]["coherence"]
    assert fields["risk"] == canonical["metrics"]["risk_score"]
    assert fields == resolve_checkin_fields(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_ins", [2, 30])
async def test_the_sdk_resolves_metrics_from_the_compact_text(handler, check_ins):
    from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
    from tests.helpers.metrics_producer import real_metrics_payload
    from unitares_sdk._metrics_fields import resolve_metrics_fields
    from unitares_sdk.client import GovernanceClient

    payload, validated = await real_metrics_payload({}, check_ins=check_ins)
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated
    )
    handler["text"] = json.dumps(env, ensure_ascii=False)

    result = await _over_mcp("call_tool", "check_working_state", {})
    text = _text(_wire(result))
    assert "\n" not in text
    assert len(text) <= len(handler["text"])

    fields = resolve_metrics_fields(GovernanceClient._parse_mcp_result(result))

    def value(node):
        return node.get("value") if isinstance(node, dict) else node

    for key in ("E", "I", "S", "V"):
        assert fields["metrics"][key] == pytest.approx(value(payload[key]), abs=1e-5), key
    assert fields["coherence"] == pytest.approx(value(payload["coherence"]), abs=1e-6)
    assert fields["risk"] == value(payload["risk_score"])
    assert fields == resolve_metrics_fields(env)
