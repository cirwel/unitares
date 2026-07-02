"""
HTTP endpoint tests using Starlette TestClient.

Tests the HTTP layer contract (JSON request/response, headers, error handling)
using a minimal test ASGI app that mirrors mcp_server.py endpoints.
"""

import json
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from mcp.types import ImageContent, TextContent
from starlette.testclient import TestClient

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.http_test_app import create_test_app
from src.http_api import (
    _build_http_tool_response,
    _normalize_http_tool_name,
    _resolve_http_bound_agent,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_dispatch():
    """Create a mock dispatch function."""
    from mcp.types import TextContent
    dispatch = AsyncMock()
    dispatch.return_value = [
        TextContent(type="text", text=json.dumps({"success": True, "tool": "test"}))
    ]
    return dispatch


@pytest.fixture
def mock_list_tools():
    """Create a mock list_tools function."""
    return lambda: [
        {"name": "health_check", "description": "Check health"},
        {"name": "list_tools", "description": "List tools"},
    ]


@pytest.fixture
def client(mock_dispatch, mock_list_tools):
    """Create a Starlette TestClient with mocked dispatch."""
    app = create_test_app(mock_dispatch, mock_list_tools)
    return TestClient(app)


# ============================================================================
# Health Endpoint
# ============================================================================

class TestHealthEndpoint:

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_ok_status(self, client):
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "ok"


# ============================================================================
# List Tools Endpoint
# ============================================================================

class TestListToolsEndpoint:

    def test_list_tools_returns_200(self, client):
        response = client.get("/v1/tools/list")
        assert response.status_code == 200

    def test_list_tools_returns_array(self, client):
        response = client.get("/v1/tools/list")
        data = response.json()
        assert "tools" in data
        assert isinstance(data["tools"], list)
        assert len(data["tools"]) == 2

    def test_list_tools_has_tool_names(self, client):
        response = client.get("/v1/tools/list")
        tools = response.json()["tools"]
        names = [t["name"] for t in tools]
        assert "health_check" in names


# ============================================================================
# Call Tool Endpoint
# ============================================================================

class TestCallToolEndpoint:

    def test_call_tool_returns_200(self, client, mock_dispatch):
        response = client.post("/v1/tools/call", json={
            "tool_name": "health_check",
            "arguments": {}
        })
        assert response.status_code == 200
        mock_dispatch.assert_called_once()

    def test_call_tool_dispatches_correct_name(self, client, mock_dispatch):
        client.post("/v1/tools/call", json={
            "tool_name": "process_agent_update",
            "arguments": {"confidence": 0.8}
        })
        call_args = mock_dispatch.call_args
        assert call_args[0][0] == "process_agent_update"

    def test_call_tool_passes_arguments(self, client, mock_dispatch):
        client.post("/v1/tools/call", json={
            "tool_name": "test_tool",
            "arguments": {"key": "value", "count": 42}
        })
        call_args = mock_dispatch.call_args
        assert call_args[0][1]["key"] == "value"
        assert call_args[0][1]["count"] == 42

    def test_call_tool_accepts_name_field(self, client, mock_dispatch):
        """Should accept 'name' as alternative to 'tool_name'."""
        client.post("/v1/tools/call", json={
            "name": "alt_tool",
            "arguments": {}
        })
        call_args = mock_dispatch.call_args
        assert call_args[0][0] == "alt_tool"

    def test_missing_tool_name_returns_400(self, client):
        response = client.post("/v1/tools/call", json={
            "arguments": {}
        })
        assert response.status_code == 400
        data = response.json()
        assert data["error"] is True

    def test_invalid_json_returns_400(self, client):
        response = client.post(
            "/v1/tools/call",
            content=b"not valid json {{{",
            headers={"content-type": "application/json"}
        )
        assert response.status_code == 400

    def test_session_header_propagated(self, client, mock_dispatch):
        """X-Session-ID header should be injected into arguments."""
        client.post(
            "/v1/tools/call",
            json={"tool_name": "test_tool", "arguments": {}},
            headers={"x-session-id": "my-session-123"}
        )
        call_args = mock_dispatch.call_args
        arguments = call_args[0][1]
        assert arguments.get("client_session_id") == "my-session-123"

    def test_explicit_client_session_id_beats_session_header(self, client, mock_dispatch):
        """HTTP header must not overwrite the session id returned by onboard()."""
        client.post(
            "/v1/tools/call",
            json={
                "tool_name": "test_tool",
                "arguments": {"client_session_id": "agent-returned-by-onboard"},
            },
            headers={"x-session-id": "custom-rest-session"},
        )
        call_args = mock_dispatch.call_args
        arguments = call_args[0][1]
        assert arguments.get("client_session_id") == "agent-returned-by-onboard"

    def test_empty_arguments_defaults_to_dict(self, client, mock_dispatch):
        """Missing arguments field should default to empty dict."""
        client.post("/v1/tools/call", json={
            "tool_name": "test_tool"
        })
        call_args = mock_dispatch.call_args
        assert isinstance(call_args[0][1], dict)


class TestHttpToolResponseSerialization:

    def test_single_text_json_preserves_legacy_result_shape(self):
        response = _build_http_tool_response(
            "health_check",
            [TextContent(type="text", text=json.dumps({"status": "ok"}))]
        )
        assert response == {
            "name": "health_check",
            "result": {"status": "ok"},
            "success": True,
        }

    def test_multiple_text_blocks_preserve_all_content(self):
        response = _build_http_tool_response(
            "multi_text",
            [
                TextContent(type="text", text="first"),
                TextContent(type="text", text="second"),
            ]
        )
        assert response["success"] is True
        assert response["result"]["content"] == [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]

    def test_non_text_content_is_not_dropped(self):
        response = _build_http_tool_response(
            "image_tool",
            [ImageContent(type="image", data="abc123", mimeType="image/png")]
        )
        assert response["success"] is True
        assert response["result"]["content"] == [
            {"type": "image", "data": "abc123", "mimeType": "image/png"}
        ]

    def test_direct_dict_result_is_preserved(self):
        response = _build_http_tool_response(
            "health_check",
            {"status": "healthy", "checks": {"db": {"status": "healthy"}}},
        )
        assert response == {
            "name": "health_check",
            "result": {"status": "healthy", "checks": {"db": {"status": "healthy"}}},
            "success": True,
        }


class TestHttpToolNameNormalization:

    def test_prefixed_mcp_tool_name_maps_to_canonical_name(self):
        assert _normalize_http_tool_name(
            {"name": "mcp_unitares_health_check"},
            "unitares",
        ) == "health_check"

    def test_tool_name_field_is_used_when_name_missing(self):
        assert _normalize_http_tool_name(
            {"tool_name": "identity"},
            "unitares",
        ) == "identity"

    def test_missing_name_returns_unknown(self):
        assert _normalize_http_tool_name({}, "unitares") == "unknown"


class TestHttpIdentityResolution:

    @pytest.mark.asyncio
    async def test_resolve_http_bound_agent_injects_resumed_uuid(self):
        arguments = {"client_session_id": "agent-123", "model_type": "gpt-5.4-codex"}
        signals = MagicMock()

        with patch("src.mcp_handlers.context.update_context_agent_id") as mock_update, \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new=AsyncMock(return_value="agent-123")), \
             patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new=AsyncMock(return_value={
                 "created": False,
                 "agent_uuid": "12345678-1234-1234-1234-123456789abc",
             })):
            resolved = await _resolve_http_bound_agent("process_agent_update", arguments, signals)

        assert resolved == "12345678-1234-1234-1234-123456789abc"
        assert arguments["agent_id"] == "12345678-1234-1234-1234-123456789abc"
        mock_update.assert_called_once_with("12345678-1234-1234-1234-123456789abc")

    @pytest.mark.asyncio
    async def test_resolve_http_bound_agent_skips_identity_tool(self):
        arguments = {"client_session_id": "agent-123"}
        resolved = await _resolve_http_bound_agent("identity", arguments, MagicMock())
        assert resolved is None
        assert "agent_id" not in arguments
