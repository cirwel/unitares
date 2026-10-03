"""Tests for gateway.tools — tool handlers."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.gateway.client import GovernanceMCPClient, MCPError, CircuitOpenError
from src.gateway.tools import (
    handle_status, handle_checkin, handle_search,
    handle_note, handle_query, handle_help,
)


@pytest.fixture
def mock_client():
    client = GovernanceMCPClient()
    client.call_tool = AsyncMock()
    return client


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_success(self, mock_client):
        mock_client.call_tool.return_value = {
            "eisv": {"E": 0.8, "I": 0.7, "S": 0.3, "V": 0.1},
            "coherence": 0.5,
            "basin": "high",
            "action": "proceed",
        }
        result = json.loads(await handle_status(mock_client))
        assert result["ok"] is True
        assert result["data"]["verdict"] == "proceed"
        mock_client.call_tool.assert_called_with("check_working_state", {})

    @pytest.mark.asyncio
    async def test_with_agent_id(self, mock_client):
        mock_client.call_tool.return_value = {"action": "proceed"}
        await handle_status(mock_client, agent_id="my-agent")
        mock_client.call_tool.assert_called_with("check_working_state", {"agent_id": "my-agent"})

    @pytest.mark.asyncio
    async def test_error(self, mock_client):
        mock_client.call_tool.side_effect = ConnectionError("refused")
        result = json.loads(await handle_status(mock_client))
        assert result["ok"] is False
        assert "Cannot reach" in result["summary"]


class TestHandleCheckin:
    @pytest.mark.asyncio
    async def test_success(self, mock_client):
        mock_client.call_tool.return_value = {
            "action": "proceed", "margin": "comfortable", "reason": "Good"
        }
        result = json.loads(await handle_checkin(mock_client, summary="Fixed bug"))
        assert result["ok"] is True
        assert result["data"]["verdict"] == "proceed"
        mock_client.call_tool.assert_called_with("sync_state", {
            "summary": "Fixed bug",
            "complexity": 0.5,
            "confidence": 0.7,
        })

    @pytest.mark.asyncio
    async def test_custom_params(self, mock_client):
        mock_client.call_tool.return_value = {"action": "guide"}
        await handle_checkin(mock_client, summary="Work", complexity=0.9, confidence=0.3)
        mock_client.call_tool.assert_called_with("sync_state", {
            "summary": "Work",
            "complexity": 0.9,
            "confidence": 0.3,
        })

    @pytest.mark.asyncio
    async def test_agent_id_forwarded(self, mock_client):
        mock_client.call_tool.return_value = {"action": "proceed"}
        await handle_checkin(mock_client, summary="Work", agent_id="perplexity-bot")
        mock_client.call_tool.assert_called_with("sync_state", {
            "summary": "Work",
            "complexity": 0.5,
            "confidence": 0.7,
            "agent_id": "perplexity-bot",
        })

    @pytest.mark.asyncio
    async def test_circuit_open(self, mock_client):
        mock_client.call_tool.side_effect = CircuitOpenError("open")
        result = json.loads(await handle_checkin(mock_client, summary="Work"))
        assert result["ok"] is False
        assert "unavailable" in result["summary"]


class TestHandleSearch:
    @pytest.mark.asyncio
    async def test_success(self, mock_client):
        mock_client.call_tool.return_value = {
            "results": [{"title": "Test", "content": "Found it"}]
        }
        result = json.loads(await handle_search(mock_client, query="test"))
        assert result["ok"] is True
        assert len(result["data"]["results"]) == 1

    @pytest.mark.asyncio
    async def test_limit(self, mock_client):
        mock_client.call_tool.return_value = {"results": []}
        await handle_search(mock_client, query="test", limit=10)
        mock_client.call_tool.assert_called_with("knowledge", {
            "action": "search", "query": "test", "limit": 10,
        })

    @pytest.mark.asyncio
    async def test_agent_filter(self, mock_client):
        mock_client.call_tool.return_value = {"results": []}
        await handle_search(mock_client, query="test", agent_id="agent-1")
        mock_client.call_tool.assert_called_with("knowledge", {
            "action": "search", "query": "test", "limit": 5, "agent_id": "agent-1",
        })


class TestHandleNote:
    @pytest.mark.asyncio
    async def test_success(self, mock_client):
        mock_client.call_tool.return_value = {"node_id": "n-1"}
        result = json.loads(await handle_note(mock_client, content="Important finding"))
        assert result["ok"] is True
        assert result["data"]["saved"] is True

    @pytest.mark.asyncio
    async def test_with_tags(self, mock_client):
        mock_client.call_tool.return_value = {"node_id": "n-2"}
        await handle_note(mock_client, content="Test", tags="redis,perf")
        mock_client.call_tool.assert_called_with("knowledge", {
            "action": "note", "summary": "Test", "tags": ["redis", "perf"],
        })

    @pytest.mark.asyncio
    async def test_routes_through_canonical_knowledge_tool(self, mock_client):
        mock_client.call_tool.return_value = {"note_id": "n-3"}
        await handle_note(mock_client, content="Important finding")
        mock_client.call_tool.assert_called_with("knowledge", {
            "action": "note", "summary": "Important finding",
        })


class TestHandleQuery:
    @pytest.mark.asyncio
    async def test_routes_to_status(self, mock_client):
        # Mock both call_model (for intent) and get_governance_metrics (for status)
        async def side_effect(tool, args=None):
            if tool == "call_model":
                return {"response": "status"}
            return {"action": "proceed", "coherence": 0.5}

        mock_client.call_tool.side_effect = side_effect
        result = json.loads(await handle_query(mock_client, question="What is my coherence?"))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_routes_to_search_on_unknown(self, mock_client):
        async def side_effect(tool, args=None):
            if tool == "call_model":
                raise Exception("unavailable")
            return {"results": []}

        mock_client.call_tool.side_effect = side_effect
        # "random gibberish" doesn't match any keyword → defaults to search
        result = json.loads(await handle_query(mock_client, question="xyzzy plugh"))
        assert result["ok"] is True


class TestHandleHelp:
    def test_returns_tools(self):
        result = json.loads(handle_help())
        assert result["ok"] is True
        tool_names = [t["name"] for t in result["data"]["tools"]]
        assert "status" in tool_names
        assert "checkin" in tool_names
        assert "query" in tool_names
        assert len(tool_names) == 6


REFUSAL = {
    "success": True,
    "status": "identity_required",
    "refused": True,
    "rollout_flag": "STRICT_IDENTITY_REQUIRED",
    "hint": "No identity resolved for this call",
}


class TestStrictIdentityRefusal:
    """A refusal is shaped like a success; the gateway must not report it as one."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [
            lambda c: handle_note(c, content="x"),
            lambda c: handle_checkin(c, summary="x"),
            lambda c: handle_search(c, query="x"),
            lambda c: handle_status(c),
        ],
        ids=["note", "checkin", "search", "status"],
    )
    async def test_refusal_is_an_error_not_a_success(self, mock_client, call):
        mock_client.call_tool.return_value = dict(REFUSAL)
        result = json.loads(await call(mock_client))
        assert result["ok"] is False
        assert "identity_required" in result["error"]
        assert "client_session_id" in result["error"]

    @pytest.mark.asyncio
    async def test_unbound_status_is_an_error(self, mock_client):
        mock_client.call_tool.return_value = {
            "success": True,
            "action_summary": {"action": "unbound", "verdict": "unbound"},
        }
        result = json.loads(await handle_status(mock_client))
        assert result["ok"] is False
        assert "Not bound" in result["summary"]

    @pytest.mark.asyncio
    async def test_non_refusal_still_succeeds(self, mock_client):
        mock_client.call_tool.return_value = {"success": True, "id": "n1"}
        result = json.loads(await handle_note(mock_client, content="x"))
        assert result["ok"] is True
        assert result["data"]["id"] == "n1"


class TestClientSessionIdPassthrough:
    @pytest.mark.asyncio
    async def test_note_forwards_client_session_id(self, mock_client):
        mock_client.call_tool.return_value = {"success": True}
        await handle_note(mock_client, content="x", client_session_id="agent-abc")
        args = mock_client.call_tool.call_args[0][1]
        assert args["client_session_id"] == "agent-abc"

    @pytest.mark.asyncio
    async def test_omitted_client_session_id_is_not_sent(self, mock_client):
        mock_client.call_tool.return_value = {"success": True}
        await handle_checkin(mock_client, summary="x")
        args = mock_client.call_tool.call_args[0][1]
        assert "client_session_id" not in args

    @pytest.mark.asyncio
    async def test_query_forwards_to_routed_tool(self, mock_client):
        mock_client.call_tool.return_value = {"success": True, "results": []}
        await handle_query(mock_client, question="what is EISV?", client_session_id="agent-abc")
        args = mock_client.call_tool.call_args[0][1]
        assert args["client_session_id"] == "agent-abc"


class TestQueryBindsClassifier:
    @pytest.mark.asyncio
    async def test_handle_query_binds_the_classification_call(self, mock_client):
        """The session id must reach the call_model classification, not only the routed tool."""
        mock_client.call_tool.return_value = {"success": True, "response": "note"}
        await handle_query(mock_client, question="keep this observation", client_session_id="agent-abc")
        first_name, first_args = mock_client.call_tool.call_args_list[0][0]
        assert first_name == "call_model"
        assert first_args["client_session_id"] == "agent-abc"
