"""Regression tests for FastMCP-registered alias wrappers.

The MCP transport registers friendly aliases as real FastMCP tools so clients
do not get "Unknown tool" before dispatch runs. Those wrappers still need to
enter dispatch under the friendly name; otherwise alias-layer normalizers and
the agent-experience envelope are bypassed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import TextContent


@pytest.mark.asyncio
async def test_sync_state_mcp_wrapper_uses_alias_middleware(monkeypatch):
    import src.mcp_handlers as handlers
    import src.mcp_server as mcp_server

    captured: dict = {}

    async def fake_process_agent_update(arguments):
        captured.update(arguments)
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "metrics": {"coherence": 0.8, "risk_score": 0.1},
                }),
            )
        ]

    monkeypatch.setitem(
        handlers.TOOL_HANDLERS,
        "process_agent_update",
        fake_process_agent_update,
    )

    monkeypatch.setattr(
        "src.mcp_handlers.identity.handlers.resolve_session_identity",
        AsyncMock(return_value={
            "agent_uuid": "test-agent-uuid",
            "agent_name": "TestAgent",
            "created": False,
            "persisted": True,
        }),
    )
    monkeypatch.setattr(
        "src.mcp_handlers.identity.handlers.lookup_onboard_pin",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.mcp_handlers.identity.handlers.derive_session_key",
        AsyncMock(return_value="test-session-key"),
    )

    limiter = MagicMock()
    limiter.check_rate_limit.return_value = (True, None)
    limiter.get_stats.return_value = {}
    monkeypatch.setattr(
        "src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter",
        lambda: limiter,
    )
    monkeypatch.setattr(
        "src.pattern_tracker.get_pattern_tracker",
        lambda: MagicMock(),
    )

    tool = mcp_server.mcp._tool_manager.get_tool("sync_state")
    result = await tool.run(
        {
            "client_session_id": "test-session",
            "response_text": "dogfood alias wrapper",
            "complexity": "medium",
            "confidence": 0.8,
        },
        convert_result=False,
    )

    assert captured["complexity"] == 0.5
    assert result["tool"] == "sync_state"
    assert result["raw_governance"]["normalized_parameters"] == {
        "complexity": {
            "from": "medium",
            "to": 0.5,
            "interpretation": "named_level",
        }
    }
