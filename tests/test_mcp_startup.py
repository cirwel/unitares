"""MCP server startup: the advertised schema list, the handlers, and the server import agree.

Until 2026-09-07 this file computed the two set differences, appended them to
an error list, and then `return`ed 1 instead of asserting, so pytest recorded
a pass while the schema list carried 23 names with no handler.
"""

import json

import pytest


def _definitions():
    from src.tool_schemas import get_tool_definitions

    return get_tool_definitions()


def test_every_advertised_schema_is_a_well_formed_object_schema():
    from src.mcp_compat import get_tool_input_schema

    tools = _definitions()
    assert tools
    for tool in tools:
        assert tool.name
        schema = get_tool_input_schema(tool)
        assert isinstance(schema, dict), tool.name
        assert schema.get("type") == "object", tool.name
        json.dumps(schema)


def test_schema_list_and_dispatch_table_name_the_same_tools():
    """Every schema has a handler and every registered handler has a schema."""
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.decorators import get_tool_registry

    schema_tools = {t.name for t in _definitions()}
    assert schema_tools == set(get_tool_registry())
    assert schema_tools == set(TOOL_HANDLERS)


def test_server_registers_list_tools():
    from src.mcp_server_std import server

    # mcp 1.x exposes the decorator method `list_tools`; 2.x registers handlers
    # by method string in `_request_handlers` ("tools/list").
    assert hasattr(server, "list_tools") or "tools/list" in getattr(server, "_request_handlers", {})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
