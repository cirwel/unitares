"""Tests for GovernanceClient (async)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceTimeoutError,
    IdentityDriftError,
)
from unitares_sdk.models import CheckinResult, IdentityResult, OnboardResult


# --- Helpers ---


@dataclass
class FakeTextContent:
    text: str


@dataclass
class FakeToolResult:
    content: list


def make_mcp_result(data: dict) -> FakeToolResult:
    """Create a fake MCP tool result that looks like what ClientSession.call_tool returns."""
    return FakeToolResult(content=[FakeTextContent(text=json.dumps(data))])


def make_client_with_session(session_mock: AsyncMock | None = None) -> GovernanceClient:
    """Create a GovernanceClient with a mocked MCP session."""
    client = GovernanceClient(timeout=5.0, retry_delay=0.01)
    client._session = session_mock or AsyncMock()
    return client


# --- Session injection ---


class TestSessionInjection:
    def test_injects_session_id(self):
        client = GovernanceClient()
        client.client_session_id = "sid-123"
        client.continuity_token = "tok-456"

        result = client._inject_session("process_agent_update", {"response_text": "hi"})
        assert result["client_session_id"] == "sid-123"
        assert result["continuity_token"] == "tok-456"
        assert result["response_text"] == "hi"

    def test_skips_injection_for_onboard(self):
        client = GovernanceClient()
        client.client_session_id = "sid-123"
        result = client._inject_session("onboard", {"name": "Test"})
        assert "client_session_id" not in result

    def test_skips_injection_for_identity(self):
        client = GovernanceClient()
        client.client_session_id = "sid-123"
        result = client._inject_session("identity", {"name": "Test"})
        assert "client_session_id" not in result

    def test_does_not_overwrite_explicit_session(self):
        client = GovernanceClient()
        client.client_session_id = "auto-sid"
        result = client._inject_session(
            "process_agent_update",
            {"response_text": "hi", "client_session_id": "explicit-sid"},
        )
        assert result["client_session_id"] == "explicit-sid"


# --- Identity capture ---


class TestIdentityCapture:
    def test_captures_top_level_fields(self):
        client = GovernanceClient()
        client._capture_identity({
            "client_session_id": "sid-1",
            "continuity_token": "tok-1",
            "uuid": "u-123",
        })
        assert client.client_session_id == "sid-1"
        assert client.continuity_token == "tok-1"
        assert client.agent_uuid == "u-123"

    def test_captures_from_session_continuity(self):
        client = GovernanceClient()
        client._capture_identity({
            "session_continuity": {
                "client_session_id": "sid-2",
                "continuity_token": "tok-2",
            },
            "bound_identity": {"uuid": "u-456"},
        })
        assert client.client_session_id == "sid-2"
        assert client.continuity_token == "tok-2"
        assert client.agent_uuid == "u-456"

    def test_captures_from_identity_summary(self):
        client = GovernanceClient()
        client._capture_identity({
            "identity_summary": {
                "client_session_id": {"value": "sid-3"},
                "continuity_token": {"value": "tok-3"},
            },
            "agent_uuid": "u-789",
        })
        assert client.client_session_id == "sid-3"
        assert client.continuity_token == "tok-3"
        assert client.agent_uuid == "u-789"

    def test_captures_from_quick_reference(self):
        client = GovernanceClient()
        client._capture_identity({
            "quick_reference": {"for_strong_resume": "tok-qr"},
        })
        assert client.continuity_token == "tok-qr"

    def test_raises_on_uuid_drift(self):
        client = GovernanceClient()
        client.agent_uuid = "original-uuid"
        with pytest.raises(IdentityDriftError) as exc_info:
            client._capture_identity({"uuid": "different-uuid"})
        assert exc_info.value.expected_uuid == "original-uuid"
        assert exc_info.value.received_uuid == "different-uuid"


# --- MCP result parsing ---


class TestMCPParsing:
    def test_parses_json_content(self):
        result = GovernanceClient._parse_mcp_result(
            make_mcp_result({"success": True, "uuid": "u-1"})
        )
        assert result["success"] is True
        assert result["uuid"] == "u-1"

    def test_merges_multiple_content_blocks(self):
        result = GovernanceClient._parse_mcp_result(
            FakeToolResult(content=[
                FakeTextContent(text='{"success": true}'),
                FakeTextContent(text='{"uuid": "u-1"}'),
            ])
        )
        assert result["success"] is True
        assert result["uuid"] == "u-1"

    def test_handles_non_json_content(self):
        result = GovernanceClient._parse_mcp_result(
            FakeToolResult(content=[FakeTextContent(text="plain text response")])
        )
        assert result["raw"] is True
        assert "plain text" in result["text"]

    def test_handles_empty_content(self):
        result = GovernanceClient._parse_mcp_result(
            FakeToolResult(content=[])
        )
        assert result["success"] is False


# --- Tool name mapping ---


class TestToolMapping:
    @pytest.mark.asyncio
    async def test_checkin_maps_to_process_agent_update(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "status": "active",
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "coherence": 0.85},
        }))
        client = make_client_with_session(session)

        result = await client.checkin("did work", complexity=0.5)
        session.call_tool.assert_called_once()
        tool_name = session.call_tool.call_args[0][0]
        assert tool_name == "process_agent_update"
        assert isinstance(result, CheckinResult)

    @pytest.mark.asyncio
    async def test_get_metrics_maps_to_get_governance_metrics(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "metrics": {},
        }))
        client = make_client_with_session(session)

        await client.get_metrics()
        tool_name = session.call_tool.call_args[0][0]
        assert tool_name == "get_governance_metrics"

    @pytest.mark.asyncio
    async def test_search_knowledge_maps_to_knowledge_action_search(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "results": [],
        }))
        client = make_client_with_session(session)

        await client.search_knowledge("test query")
        args = session.call_tool.call_args[0][1]
        assert args["action"] == "search"
        assert args["query"] == "test query"

    @pytest.mark.asyncio
    async def test_store_discovery_maps_to_knowledge_action_store(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
        }))
        client = make_client_with_session(session)

        await client.store_discovery(
            summary="found a bug",
            discovery_type="bug_found",
            severity="critical",
            tags=["watcher"],
        )
        args = session.call_tool.call_args[0][1]
        assert args["action"] == "store"
        assert args["discovery_type"] == "bug_found"
        assert args["severity"] == "critical"
        assert args["tags"] == ["watcher"]

    @pytest.mark.asyncio
    async def test_call_model_passes_none_provider_through(self):
        """When provider/model are None, they should not appear in args (server decides)."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "response": "hello",
        }))
        client = make_client_with_session(session)

        await client.call_model("test prompt")
        args = session.call_tool.call_args[0][1]
        assert "provider" not in args
        assert "model" not in args
        assert args["prompt"] == "test prompt"


# --- Kwargs passthrough ---


class TestKwargsPassthrough:
    @pytest.mark.asyncio
    async def test_onboard_passes_extra_kwargs(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.onboard("Test", custom_field="custom_value")
        args = session.call_tool.call_args[0][1]
        assert args["custom_field"] == "custom_value"

    @pytest.mark.asyncio
    async def test_onboard_forwards_parent_agent_id_and_spawn_reason(self):
        """Typed lineage params reach server args dict when provided."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.onboard(
            "Test",
            parent_agent_id="parent-uuid-abc",
            spawn_reason="subagent",
        )
        args = session.call_tool.call_args[0][1]
        assert args["parent_agent_id"] == "parent-uuid-abc"
        assert args["spawn_reason"] == "subagent"

    @pytest.mark.asyncio
    async def test_onboard_omits_lineage_when_none(self):
        """Default None values must not appear in server args (backward compat)."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.onboard("Test")
        args = session.call_tool.call_args[0][1]
        assert "parent_agent_id" not in args
        assert "spawn_reason" not in args

    @pytest.mark.asyncio
    async def test_identity_captures_resident_name_from_kwarg(self):
        """RFC §7.13 (regression for 2026-05-04 multi-resident canary):
        identity() MUST capture resident_name like onboard() does. Without
        this, substrate-anchored residents (Vigil/Sentinel/Watcher/Chronicler)
        that resume via identity() never set resident_name and the
        post-checkin substrate emission silently skips."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.identity(name="Sentinel", agent_uuid="some-uuid", resume=True)
        assert client.resident_name == "Sentinel"

    @pytest.mark.asyncio
    async def test_identity_captures_resident_name_from_response_label(self):
        """If caller doesn't pass name kwarg, fall back to label field on the
        identity response. Lets the SDK still capture the name on UUID-only
        resume calls when the server includes label in the response."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
            "label": "Vigil",
        }))
        client = make_client_with_session(session)

        await client.identity(agent_uuid="some-uuid", resume=True)
        assert client.resident_name == "Vigil"

    @pytest.mark.asyncio
    async def test_identity_resident_name_stays_none_when_unresolvable(self):
        """No name kwarg AND no label in response → resident_name stays None.
        This is the non-resident caller path; substrate emission gates on
        non-None resident_name."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.identity(agent_uuid="some-uuid", resume=True)
        assert client.resident_name is None

    @pytest.mark.asyncio
    async def test_identity_forwards_parent_agent_id_and_spawn_reason(self):
        """Typed lineage params also flow through identity() for creation fallthrough."""
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-1",
            "uuid": "u-1",
        }))
        client = make_client_with_session(session)

        await client.identity(
            name="Test",
            parent_agent_id="parent-uuid-xyz",
            spawn_reason="compaction",
        )
        args = session.call_tool.call_args[0][1]
        assert args["parent_agent_id"] == "parent-uuid-xyz"
        assert args["spawn_reason"] == "compaction"


# --- Timeout and retry ---


class TestTimeoutAndRetry:
    @pytest.mark.asyncio
    async def test_raises_timeout_error(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        client = make_client_with_session(session)
        client._session = session

        # Mock wait_for to raise TimeoutError
        with pytest.raises(GovernanceTimeoutError):
            await client.call_tool("test_tool", {})

    @pytest.mark.asyncio
    async def test_raises_connection_error(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(side_effect=ConnectionError("refused"))
        client = make_client_with_session(session)

        with pytest.raises(GovernanceConnectionError):
            await client.call_tool("test_tool", {})

    @pytest.mark.asyncio
    async def test_retries_once_on_transient_error(self):
        """Should retry once, then succeed."""
        session = AsyncMock()
        session.call_tool = AsyncMock(
            side_effect=[
                ConnectionError("first attempt"),
                make_mcp_result({"success": True}),
            ]
        )
        client = make_client_with_session(session)

        result = await client.call_tool("test_tool", {})
        assert result["success"] is True
        assert session.call_tool.call_count == 2


# --- Onboard response handling ---


class TestOnboard:
    @pytest.mark.asyncio
    async def test_onboard_captures_identity(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "client_session_id": "sid-new",
            "uuid": "u-new",
            "continuity_token": "v1.tok.sig",
            "continuity_token_supported": True,
            "is_new": True,
            "welcome": "Hello",
        }))
        client = make_client_with_session(session)

        result = await client.onboard("TestAgent")
        assert isinstance(result, OnboardResult)
        assert result.client_session_id == "sid-new"
        assert client.client_session_id == "sid-new"
        assert client.agent_uuid == "u-new"
        assert client.continuity_token == "v1.tok.sig"


# --- Checkin verdict handling ---


class TestCheckinVerdict:
    @pytest.mark.asyncio
    async def test_proceed_verdict(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "status": "active",
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "coherence": 0.85},
        }))
        client = make_client_with_session(session)

        result = await client.checkin("test work")
        assert result.verdict == "proceed"
        assert result.coherence == 0.85

    @pytest.mark.asyncio
    async def test_guide_verdict_with_margin(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": True,
            "status": "active",
            "decision": {"action": "guide", "guidance": "Watch entropy"},
            "metrics": {"coherence": 0.45},
            "margin": "tight",
        }))
        client = make_client_with_session(session)

        result = await client.checkin("test work")
        assert result.verdict == "guide"
        assert result.guidance == "Watch entropy"

    @pytest.mark.asyncio
    async def test_checkin_failure_raises_connection_error(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": False,
            "error": "governance down",
        }))
        client = make_client_with_session(session)

        with pytest.raises(GovernanceConnectionError, match="governance down"):
            await client.checkin("test work")


class TestSearchKnowledgeFailure:
    @pytest.mark.asyncio
    async def test_search_failure_raises_connection_error(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=make_mcp_result({
            "success": False,
            "error": "search unavailable",
        }))
        client = make_client_with_session(session)

        with pytest.raises(GovernanceConnectionError, match="search unavailable"):
            await client.search_knowledge("test query")


# --- Not connected ---


class TestNotConnected:
    @pytest.mark.asyncio
    async def test_call_tool_without_connect_raises(self):
        client = GovernanceClient()
        with pytest.raises(GovernanceConnectionError, match="Not connected"):
            await client.call_tool("onboard", {})


# --- Connect failure cleanup ---


class TestConnectFailureCleanup:
    """Regression tests for the sentinel crash (KG 2026-04-19T00:51:46).

    When connect() fails partway, the partially-entered context managers must
    be unwound before the exception propagates. Otherwise Python skips
    __aexit__, the MCP streamable_http_client's anyio task group is leaked,
    and it crashes at GC with "Attempted to exit cancel scope in a different
    task than it was entered in".
    """

    @pytest.mark.asyncio
    async def test_initialize_failure_unwinds_cm_stack_and_http_client(self):
        """session.initialize() failure must leave client in a clean state."""
        client = GovernanceClient()

        entered_cm = AsyncMock()
        entered_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
        entered_cm.__aexit__ = AsyncMock(return_value=None)

        entered_session_cm = AsyncMock()
        session_mock = AsyncMock()
        session_mock.initialize = AsyncMock(side_effect=ConnectionError("upstream down"))
        entered_session_cm.__aenter__ = AsyncMock(return_value=session_mock)
        entered_session_cm.__aexit__ = AsyncMock(return_value=None)

        http_client_mock = MagicMock()
        http_client_mock.aclose = AsyncMock()

        with (
            patch("unitares_sdk.client.httpx.AsyncClient", return_value=http_client_mock),
            patch("unitares_sdk.client.streamable_http_client", return_value=entered_cm),
            patch("unitares_sdk.client.ClientSession", return_value=entered_session_cm),
        ):
            with pytest.raises(ConnectionError, match="upstream down"):
                await client.connect()

        # Original exception propagates; cleanup ran.
        assert client._cm_stack == []
        assert client._session is None
        assert client._http_client is None
        entered_cm.__aexit__.assert_called_once()
        entered_session_cm.__aexit__.assert_called_once()
        http_client_mock.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_transport_failure_unwinds_http_client(self):
        """streamable_http_client.__aenter__ failure still closes the httpx client."""
        client = GovernanceClient()

        failing_cm = AsyncMock()
        failing_cm.__aenter__ = AsyncMock(side_effect=ConnectionError("transport refused"))
        failing_cm.__aexit__ = AsyncMock(return_value=None)

        http_client_mock = MagicMock()
        http_client_mock.aclose = AsyncMock()

        with (
            patch("unitares_sdk.client.httpx.AsyncClient", return_value=http_client_mock),
            patch("unitares_sdk.client.streamable_http_client", return_value=failing_cm),
        ):
            with pytest.raises(ConnectionError, match="transport refused"):
                await client.connect()

        assert client._cm_stack == []
        assert client._http_client is None
        # Transport CM never registered on _cm_stack (its __aenter__ raised), so no __aexit__.
        http_client_mock.aclose.assert_called_once()
