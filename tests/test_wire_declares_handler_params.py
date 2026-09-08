"""A parameter a handler reads must be declared on the tool's wire schema.

FastMCP builds its argument model from the registered schema and, by default,
drops any key that model does not name, before dispatch and before the
server's own validation. A handler that reads such a key works over REST and
in-process, where undeclared keys are merged back, and silently ignores it
over the MCP wire. Two cases sat in that state until 2026-09-07:
observe(action="telemetry") read window_hours / include_calibration, and
describe_tool read include_schema / include_full_description.
"""

from __future__ import annotations

import pytest

CASES = {
    "observe": {"window_hours": 48, "include_calibration": True},
    "describe_tool": {"include_schema": False, "include_full_description": False},
}


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
    required_example = {"observe": {"action": "telemetry"}, "describe_tool": {"tool_name": "knowledge"}}[tool_name]
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
