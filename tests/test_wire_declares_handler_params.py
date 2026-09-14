"""A parameter a handler reads must be declared on the tool's wire schema.

FastMCP builds its argument model from the registered schema and, by default,
drops any key that model does not name, before dispatch and before the
server's own validation. A handler that reads such a key works over REST and
in-process, where undeclared keys are merged back, and silently ignores it
over the MCP wire. Two cases sat in that state until 2026-09-07:
observe(action="telemetry") read window_hours / include_calibration, and
describe_tool read include_schema / include_full_description. cirs_protocol's
selectable protocol handlers read twenty-four more until #2183; list_tools
read lite / include_advanced / tier the same way, missed by that sweep.
tests/test_cirs_protocol_wire_params.py holds that schema to the handlers'
source, and the representative keys below keep the registered argument model
covered from here.
"""

from __future__ import annotations

import inspect
import json

import pytest
from mcp.types import TextContent

CASES = {
    "observe": {"window_hours": 48, "include_calibration": True},
    "describe_tool": {"include_schema": False, "include_full_description": False},
    "cirs_protocol": {"since_hours": 5.0, "action_id": "abc", "trust_default": "full"},
    "list_tools": {"lite": False, "include_advanced": False, "tier": "essential"},
    "knowledge": {"agent_id_filter": "author-agent"},
}


LIST_TOOLS_BOOLEAN_CASES = [
    pytest.param(True, True, id="bool-true"),
    pytest.param(False, False, id="bool-false"),
    pytest.param("true", True, id="string-true"),
    pytest.param("false", False, id="string-false"),
    pytest.param("1", True, id="string-1"),
    pytest.param("0", False, id="string-0"),
    pytest.param("yes", True, id="string-yes"),
    pytest.param("no", False, id="string-no"),
    pytest.param("on", True, id="string-on"),
    pytest.param("off", False, id="string-off"),
    pytest.param("  TrUe  ", True, id="whitespace-case-true"),
    pytest.param("  oFf  ", False, id="whitespace-case-false"),
    pytest.param(None, True, id="none-preserves-true-default"),
    pytest.param("not-a-boolean", True, id="invalid-preserves-true-default"),
]


def _run_args(run_method, arguments):
    """Positional args for internal Tool.run across MCP SDK 1.x/2.x."""
    params = inspect.signature(run_method).parameters
    if "context" in params and params["context"].default is inspect.Parameter.empty:
        return (arguments, None)
    return (arguments,)


@pytest.mark.parametrize("tool_name", sorted(CASES))
def test_wire_schema_declares_the_parameters(tool_name):
    from src.tool_schemas import get_tool_definitions
    from src.mcp_compat import get_tool_input_schema

    schema = {t.name: get_tool_input_schema(t) for t in get_tool_definitions()}[tool_name]
    assert set(CASES[tool_name]) <= set(schema["properties"])


@pytest.mark.parametrize("tool_name", sorted(CASES))
def test_fastmcp_argument_model_keeps_the_parameters(tool_name):
    """The registered FastMCP tool, not just the schema builder."""
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None
    required_example = {
        "observe": {"action": "telemetry"},
        "describe_tool": {"tool_name": "knowledge"},
        "cirs_protocol": {"protocol": "void_alert", "action": "query"},
        "list_tools": {},
        "knowledge": {"action": "search"},
    }[tool_name]
    validated = tool.fn_metadata.arg_model.model_validate({**required_example, **CASES[tool_name]})
    dumped = validated.model_dump_one_level() if hasattr(validated, "model_dump_one_level") else validated.model_dump()
    for key, value in CASES[tool_name].items():
        assert dumped[key] == value, key


def test_observe_telemetry_window_defaults_like_the_handler():
    from src.mcp_handlers.schemas.observability import ObserveParams

    assert ObserveParams(action="telemetry").window_hours == 24.0
    assert ObserveParams(action="telemetry", window_hours=48).window_hours == 48
    assert ObserveParams(action="agent", target_agent_id="x").window_hours is None
    assert ObserveParams(action="telemetry").include_calibration is False


def test_describe_tool_switches_coerce_like_lite():
    from src.mcp_handlers.schemas.admin import DescribeToolParams

    p = DescribeToolParams(tool_name="knowledge", include_schema="false", include_full_description="0")
    assert p.include_schema is False and p.include_full_description is False
    assert DescribeToolParams(tool_name="knowledge").include_schema is True


def test_list_tools_switches_default_like_the_handler():
    from src.mcp_handlers.schemas.admin import ListToolsParams

    defaults = ListToolsParams()
    assert defaults.lite is True
    assert defaults.include_advanced is True
    assert defaults.tier == "all"


@pytest.mark.parametrize("field_name", ["lite", "include_advanced"])
@pytest.mark.parametrize(("raw", "expected"), LIST_TOOLS_BOOLEAN_CASES)
def test_list_tools_true_default_switches_coerce_like_the_handler(
    field_name, raw, expected
):
    from src.mcp_handlers.schemas.admin import ListToolsParams
    from src.mcp_handlers.support.coerce import coerce_bool

    params = ListToolsParams(**{field_name: raw})
    assert getattr(params, field_name) is expected
    assert getattr(params, field_name) is coerce_bool(raw, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("on", id="on"),
        pytest.param("  TrUe  ", id="whitespace-case"),
        pytest.param(None, id="none"),
        pytest.param("not-a-boolean", id="invalid"),
        pytest.param("off", id="off"),
    ],
)
async def test_list_tools_mcp_dispatch_matches_direct_handler_coercion(
    monkeypatch, raw
):
    """Exercise FastMCP's live argument model and the dispatch validator.

    The direct handler uses ``coerce_bool(value, True)`` for both switches;
    schema validation must hand it the same value instead of changing the
    meaning before the handler runs.
    """
    import src.mcp_handlers as handlers
    import src.tool_registration as registration
    from src import mcp_server
    from src.mcp_handlers.support.coerce import coerce_bool

    delivered = {}

    async def capture(arguments):
        delivered.update(arguments)
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setitem(handlers.TOOL_HANDLERS, "list_tools", capture)
    monkeypatch.setattr(registration, "record_tool_usage", lambda **kwargs: None)

    tool = mcp_server.mcp._tool_manager.get_tool("list_tools")
    await tool.run(
        *_run_args(
            tool.run,
            {"lite": raw, "include_advanced": raw},
        ),
        convert_result=False,
    )

    expected = coerce_bool(raw, True)
    assert delivered["lite"] is expected
    assert delivered["include_advanced"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "all"),
        ("", "all"),
        ("  ALL  ", "all"),
        ("  EsSeNtIaL  ", "essential"),
        (" COMMON ", "common"),
        ("advanced", "advanced"),
    ],
)
async def test_list_tools_tier_is_normalized_consistently(raw, expected):
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    response = json.loads((await handle_list_tools({"lite": False, "tier": raw}))[0].text)
    assert response["success"] is True
    assert response["filter_applied"]["tier_filter"] == expected
    assert response["tools"], "a valid tier must not silently empty the catalog"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["bogus", 0, False, []])
async def test_list_tools_unknown_tier_returns_validation_error(raw):
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    response = json.loads((await handle_list_tools({"lite": False, "tier": raw}))[0].text)
    assert response["success"] is False
    assert response["error_category"] == "validation_error"
    assert "tier" in response["error"].lower()
