"""Transport catalog negotiation preserves SDK calls on progressive servers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

from mcp.client.session import ClientSession
from mcp.types import ListToolsResult, PaginatedRequestParams

import pytest

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import GovernanceConnectionError


def page(*names, cursor=None):
    return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names], nextCursor=cursor)


@pytest.mark.asyncio
async def test_paginated_catalog_routes_hidden_calls_and_keeps_identity():
    client = GovernanceClient()
    client.client_session_id = "caller-session"
    session = client._session = AsyncMock()
    session.list_tools.side_effect = [page("sync_state", cursor="next"), page("use_tool")]
    session.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"success":true,"count":3}')])
    await client._discover_tools()
    result = await client.call_tool("knowledge", {"action": "stats"})
    assert result["count"] == 3
    session.list_tools.assert_any_await(params=PaginatedRequestParams(cursor="next"))
    session.call_tool.assert_awaited_once_with("use_tool", {
        "tool_name": "knowledge",
        "arguments": {"action": "stats", "client_session_id": "caller-session"},
        "client_session_id": "caller-session",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("knowledge", "use_tool"), ("knowledge",)])
async def test_full_and_legacy_catalogs_dispatch_directly(names):
    client = GovernanceClient()
    session = client._session = AsyncMock()
    session.list_tools.return_value = page(*names)
    session.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"success":true}')])
    await client._discover_tools()
    await client.call_tool("knowledge", {"action": "stats"})
    session.call_tool.assert_awaited_once_with("knowledge", {"action": "stats"})


@pytest.mark.asyncio
async def test_gateway_failure_is_not_reported_as_success():
    client = GovernanceClient()
    session = client._session = AsyncMock()
    session.list_tools.return_value = page("use_tool")
    session.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"success":false,"error":"refused"}')])
    await client._discover_tools()
    with pytest.raises(GovernanceConnectionError, match="refused"):
        await client.call_tool("knowledge", {"action": "stats"})


@pytest.mark.asyncio
async def test_disconnect_discards_previous_server_catalog():
    client = GovernanceClient()
    client._advertised_tools = {"use_tool"}
    await client.disconnect()
    assert client._advertised_tools == set()


@pytest.mark.asyncio
async def test_discovery_uses_installed_mcp_signature_and_models():
    """Run under both supported MCP majors, including their field aliases."""
    client = GovernanceClient()
    session = client._session = create_autospec(ClientSession, instance=True)
    session.list_tools.side_effect = [
        ListToolsResult.model_validate({"tools": [], "nextCursor": "page-two"}),
        ListToolsResult.model_validate({"tools": [
            {"name": "use_tool", "inputSchema": {"type": "object"}},
        ]}),
    ]
    await client._discover_tools()
    assert client._advertised_tools == {"use_tool"}
    assert session.list_tools.await_count == 2
    session.list_tools.assert_any_await(params=PaginatedRequestParams(cursor="page-two"))
