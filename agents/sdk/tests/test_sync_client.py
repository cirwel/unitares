"""Tests for SyncGovernanceClient."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceTimeoutError,
    IdentityDriftError,
)
from unitares_sdk.models import CheckinResult, ModelResult, NoteResult, OnboardResult
from unitares_sdk.sync_client import SyncGovernanceClient


# --- Helpers ---


def _mock_urlopen(response_data: dict, status: int = 200):
    """Create a mock for urllib.request.urlopen that returns response_data as JSON."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = status
    return mock_resp


# --- REST envelope parsing ---


class TestRESTEnvelope:
    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_dict_result(self, mock_open):
        """Core tools return result as a plain dict."""
        mock_open.return_value = _mock_urlopen({
            "name": "onboard",
            "result": {
                "success": True,
                "client_session_id": "sid-1",
                "uuid": "u-1",
            },
            "success": True,
        })
        client = SyncGovernanceClient(transport="rest")
        raw = client.call_tool("onboard", {"name": "Test"})
        assert raw["success"] is True
        assert raw["client_session_id"] == "sid-1"

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_string_result(self, mock_open):
        """Some tools may return a JSON string that needs parsing."""
        mock_open.return_value = _mock_urlopen({
            "name": "test",
            "result": '{"success": true, "data": "hello"}',
            "success": True,
        })
        client = SyncGovernanceClient(transport="rest")
        raw = client.call_tool("test", {})
        assert raw["success"] is True
        assert raw["data"] == "hello"

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_failure_envelope(self, mock_open):
        """When success=false in envelope, should raise."""
        mock_open.return_value = _mock_urlopen({
            "success": False,
            "error": "Tool not found",
        })
        client = SyncGovernanceClient(transport="rest")
        with pytest.raises(GovernanceConnectionError, match="Tool not found"):
            client.call_tool("bad_tool", {})

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_multi_content_result(self, mock_open):
        """Multi-content-block result."""
        mock_open.return_value = _mock_urlopen({
            "name": "test",
            "result": {
                "content": [
                    {"type": "text", "text": '{"part": "one"}'},
                    {"type": "text", "text": '{"part2": "two"}'},
                ]
            },
            "success": True,
        })
        client = SyncGovernanceClient(transport="rest")
        raw = client.call_tool("test", {})
        assert raw["part"] == "one"
        assert raw["part2"] == "two"

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_null_result(self, mock_open):
        """Null result raises GovernanceConnectionError."""
        mock_open.return_value = _mock_urlopen({
            "name": "test",
            "result": None,
            "success": True,
        })
        client = SyncGovernanceClient(transport="rest")
        with pytest.raises(GovernanceConnectionError, match="No result"):
            client.call_tool("test", {})

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_mcp_is_error_flag(self, mock_open):
        """MCP isError on inner result raises even when outer envelope succeeds."""
        mock_open.return_value = _mock_urlopen({
            "name": "test",
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": "session not found"},
                ],
            },
            "success": True,
        })
        client = SyncGovernanceClient(transport="rest")
        with pytest.raises(GovernanceConnectionError, match="session not found"):
            client.call_tool("test", {})


# --- Session injection ---


class TestSyncSessionInjection:
    def test_injects_session_id(self):
        client = SyncGovernanceClient(transport="rest")
        client.client_session_id = "sid-123"
        client.continuity_token = "tok-456"
        result = client._inject_session("process_agent_update", {"response_text": "hi"})
        assert result["client_session_id"] == "sid-123"
        assert "continuity_token" not in result

    def test_skips_for_identity_tools(self):
        client = SyncGovernanceClient(transport="rest")
        client.client_session_id = "sid-123"
        assert "client_session_id" not in client._inject_session("onboard", {})
        assert "client_session_id" not in client._inject_session("identity", {})


# --- Identity capture ---


class TestSyncIdentityCapture:
    def test_captures_identity(self):
        client = SyncGovernanceClient(transport="rest")
        client._capture_identity({
            "client_session_id": "sid-1",
            "uuid": "u-1",
            "continuity_token": "tok-1",
        })
        assert client.client_session_id == "sid-1"
        assert client.agent_uuid == "u-1"

    def test_raises_on_drift(self):
        client = SyncGovernanceClient(transport="rest")
        client.agent_uuid = "old-uuid"
        with pytest.raises(IdentityDriftError):
            client._capture_identity({"uuid": "new-uuid"})


# --- Typed method tool mapping ---


class TestSyncToolMapping:
    def test_checkin_maps_to_process_agent_update(self):
        client = SyncGovernanceClient(transport="rest")
        calls = []

        def fake_call(tool_name, arguments, **kwargs):
            calls.append(tool_name)
            return {
                "success": True,
                "decision": {"action": "proceed"},
                "metrics": {},
            }

        client.call_tool = fake_call
        result = client.checkin("test")
        assert calls[-1] == "process_agent_update"
        assert isinstance(result, CheckinResult)

    def test_get_metrics_maps_to_get_governance_metrics(self):
        client = SyncGovernanceClient(transport="rest")
        calls = []

        def fake_call(tool_name, arguments, **kwargs):
            calls.append(tool_name)
            return {"success": True, "metrics": {}}

        client.call_tool = fake_call
        client.get_metrics()
        assert calls[-1] == "get_governance_metrics"

    def test_call_model_omits_none_provider(self):
        client = SyncGovernanceClient(transport="rest")
        captured_args = []

        def fake_call(tool_name, arguments, **kwargs):
            captured_args.append(arguments)
            return {"success": True, "response": "hi"}

        client.call_tool = fake_call
        client.call_model("test prompt")
        assert "provider" not in captured_args[0]
        assert "model" not in captured_args[0]

    def test_checkin_failure_raises_connection_error(self):
        client = SyncGovernanceClient(transport="rest")

        def fake_call(tool_name, arguments, **kwargs):
            return {"success": False, "error": "governance down"}

        client.call_tool = fake_call
        with pytest.raises(GovernanceConnectionError, match="governance down"):
            client.checkin("test")

    def test_search_failure_raises_connection_error(self):
        client = SyncGovernanceClient(transport="rest")

        def fake_call(tool_name, arguments, **kwargs):
            return {"success": False, "error": "search unavailable"}

        client.call_tool = fake_call
        with pytest.raises(GovernanceConnectionError, match="search unavailable"):
            client.search_knowledge("test")


# --- MCP transport guard ---


class TestMCPTransportGuard:
    def test_transport_attribute(self):
        client = SyncGovernanceClient(transport="mcp")
        assert client.transport == "mcp"


# --- Connection error ---


class TestSyncConnectionError:
    def test_unreachable_server(self):
        client = SyncGovernanceClient(
            rest_url="http://127.0.0.1:1/v1/tools/call",
            transport="rest",
            timeout=1.0,
        )
        with pytest.raises(GovernanceConnectionError):
            client.call_tool("test", {})

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_timeout_wrapped_in_urlerror_raises_timeout(self, mock_open):
        client = SyncGovernanceClient(transport="rest", timeout=1.0)
        mock_open.side_effect = urllib.error.URLError(TimeoutError("timed out"))

        with pytest.raises(GovernanceTimeoutError, match="timed out after 1.0s"):
            client.call_tool("test", {})
