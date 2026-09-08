"""The FastMCP mount advertises by mode and dispatches by registration.

Until the 2026-09 surface cut the /mcp/ registrars applied GOVERNANCE_TOOL_MODE
at registration time, so a register=True handler outside the mode was
"Unknown tool" on the streamable-HTTP transport while REST and stdio dispatched
it fine. These tests pin the decoupled shape: every handler and every workflow
alias is registered whatever the mode, and only tools/list is filtered - by the
same predicate REST and stdio use (get_public_tool_definitions).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.mcp_handlers  # noqa: F401  (settles the decorator registry)
from src.tool_mode_listing import (
    apply_listed_schema_policy,
    advertised_tool_names,
    filter_listed_tools,
    mode_filtered_server_class,
)
from src.tool_modes import LITE_MODE_TOOLS, MINIMAL_MODE_TOOLS

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def test_full_mode_is_unfiltered():
    assert advertised_tool_names("full") is None


def test_minimal_advertises_exactly_the_five():
    assert advertised_tool_names("minimal") == MINIMAL_MODE_TOOLS
    assert len(MINIMAL_MODE_TOOLS) == 5


def test_lite_advertises_lite_mode_tools():
    assert advertised_tool_names("lite") == LITE_MODE_TOOLS


def test_mode_is_read_at_call_time(monkeypatch):
    """A process that changes the mode sees it on the next listing."""
    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "minimal")
    assert advertised_tool_names() == MINIMAL_MODE_TOOLS
    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "full")
    assert advertised_tool_names() is None


def test_filter_keeps_order_and_drops_unadvertised():
    tools = [
        SimpleNamespace(name=name)
        for name in ("knowledge", "sync_state", "list_tools", "start_session")
    ]
    kept = [tool.name for tool in filter_listed_tools(tools, "minimal")]
    assert kept == ["sync_state", "start_session"]
    assert [tool.name for tool in filter_listed_tools(tools, "full")] == [
        "knowledge", "sync_state", "list_tools", "start_session",
    ]


@pytest.mark.asyncio
async def test_subclass_filters_list_tools_only(monkeypatch):
    """The override touches list_tools; the base class's tools stay put."""

    class FakeServer:
        def __init__(self):
            self.tools = [
                SimpleNamespace(name=name)
                for name in ("start_session", "knowledge", "identity", "list_tools")
            ]

        async def list_tools(self):
            return list(self.tools)

    server_class = mode_filtered_server_class(FakeServer)
    assert server_class.__name__ == "ModeFilteredFakeServer"
    server = server_class()

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "minimal")
    assert [tool.name for tool in await server.list_tools()] == [
        "start_session", "identity",
    ]
    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "full")
    assert len(await server.list_tools()) == 4
    assert len(server.tools) == 4


@pytest.mark.asyncio
async def test_live_mount_lists_by_mode_and_registers_everything(monkeypatch):
    """tools/list equals the mode's public surface; registration ignores the mode.

    The second half is the transport fix: names outside the mode - the lite
    routers, the recovery tool, the lite-only workflow aliases - are all
    registered with FastMCP, so a caller that knows the name (the SDK, the
    governance plugin, a resident) dispatches on /mcp/ under minimal exactly
    as it does on REST.
    """
    from src import mcp_server
    from src.interface_contract import get_public_tool_definitions
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import list_all_aliases

    registered = set(mcp_server.mcp._tool_manager._tools)
    # A governance_mcp.plugins package installed on the machine registers its
    # tools into the same FastMCP instance at import time. The fixture lifts
    # them out of the decorator registry, so anything registered with FastMCP
    # that is neither a first-party handler nor an alias is foreign here.
    foreign = registered - set(get_tool_registry()) - set(list_all_aliases())

    for mode in ("minimal", "standard", "lite", "full"):
        monkeypatch.setattr("src.tool_modes.TOOL_MODE", mode)
        listed = {tool.name for tool in await mcp_server.mcp.list_tools()}
        expected = {tool.name for tool in get_public_tool_definitions(mode)}
        assert listed - foreign == expected, mode
        assert listed <= registered

    assert {tool.name for tool in get_public_tool_definitions("full")} <= registered
    for name in ("knowledge", "self_recovery", "archive_orphan_agents",
                 "search_shared_memory", "request_review", "store_finding",
                 "list_tools", "describe_tool", "onboard", "bind_session"):
        assert name in registered, f"{name} must dispatch on /mcp/ in every mode"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["minimal", "standard", "lite", "full"])
async def test_actual_listing_title_savings_preserve_validation_and_restore(monkeypatch, mode):
    """Test the final MCP definitions, after typed-wrapper regeneration."""
    import json
    from src import mcp_server
    from src.mcp_compat import get_tool_input_schema
    from src.schema_brief import apply_property_title_mode

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", mode)
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "keep")
    before = {t.name: get_tool_input_schema(t) for t in await mcp_server.mcp.list_tools()}
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "strip")
    after = {t.name: get_tool_input_schema(t) for t in await mcp_server.mcp.list_tools()}
    assert after == {name: apply_property_title_mode(schema, "strip") for name, schema in before.items()}
    assert len(json.dumps(after)) < len(json.dumps(before))
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "keep")
    restored = {t.name: get_tool_input_schema(t) for t in await mcp_server.mcp.list_tools()}
    assert restored == before, "listing must not mutate registration/validation schemas"


def test_listing_preserves_a_parameter_named_title_and_title_in_caller_data(monkeypatch):
    from mcp.types import Tool
    from src.mcp_compat import get_tool_input_schema

    schema = {"title": "Arguments", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string", "default": "hello"},
        "data": {"type": "object", "default": {"title": "caller data"}},
    }}
    tool = Tool(name="example", inputSchema=schema)
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", "strip")
    listed = apply_listed_schema_policy([tool])[0]
    assert get_tool_input_schema(listed)["properties"]["title"]["default"] == "hello"
    assert get_tool_input_schema(listed)["properties"]["data"]["default"] == {"title": "caller data"}
    assert get_tool_input_schema(tool) == schema
