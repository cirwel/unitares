"""MCP server startup: the advertised schema list, the handlers, and the server import agree.

Until 2026-09-07 this file computed the two set differences, appended them to
an error list, and then `return`ed 1 instead of asserting, so pytest recorded
a pass while the schema list carried 23 names with no handler.
"""

import json

import pytest

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


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


def test_server_advertises_the_default_profile_and_still_dispatches_the_rest():
    """The default listing is the standard profile; registration is unfiltered.

    A mode filters tools/list only. If these two ever converge, an unadvertised
    capability has become an uncallable one.
    """
    import asyncio

    from src import mcp_server
    from src.tool_modes import STANDARD_MODE_TOOLS, TOOL_MODE

    assert TOOL_MODE == "full"
    listed = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    registered = set(mcp_server.mcp._tool_manager._tools)
    assert STANDARD_MODE_TOOLS <= listed
    assert listed <= registered
    for name in ("observe", "list_tools", "onboard"):
        assert name in listed, f"{name} must be discoverable"
        assert name in registered, f"{name} must still dispatch by name"
    # self_recovery moved into the default profile: the server names it to
    # paused agents (mcp_handlers/updates/phases.py, support/agent_auth.py),
    # so a schema-driven client has to be able to see it.
    assert "self_recovery" in listed
    assert "self_recovery" in registered
    # dialectic followed on 2026-09-08, for the same reason one step on:
    # request_review pins action="request", so without the router the six
    # actions that FINISH a review were unreachable -- and the server tells the
    # agent to call them anyway (dialectic/handlers.py:1385 and the shared
    # envelope middleware). See
    # tests/test_lite_wire_surface.py::test_standard_can_finish_the_review_it_can_start.
    assert "dialectic" in listed
    assert "dialectic" in registered
    # Ordinary detail/diagnostic hints must be usable by schema-driven clients.
    assert {"knowledge", "describe_tool", "health_check"} <= listed


def test_server_carries_instructions_naming_the_unadvertised_surface():
    """Clients get the orientation in the initialize response, not per call."""
    from src import mcp_server
    from src.tool_modes import build_server_instructions

    instructions = getattr(mcp_server.mcp, "instructions", None)
    assert instructions, "the server must ship an instructions string"
    assert instructions == build_server_instructions()
    assert "start_session" in instructions
    assert "complete catalog" in instructions


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
