"""Strict onboard proof contract.

Under STRICT_IDENTITY_REQUIRED, onboard() must not silently turn an ambiguous
no-proof call into force_new=true. Fresh identity creation is still allowed, but
it must be caller-declared with force_new=true or lineage-declared with
parent_agent_id.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_strict_bare_onboard_refuses_instead_of_auto_force_new(monkeypatch):
    """No proof + no explicit force_new is ambiguous under strict identity.

    Regression guard for the Hermes MCP churn class: server-inferred or missing
    continuity must not be laundered into a durable mint by onboard's legacy
    "arg-less means force_new" default while STRICT_IDENTITY_REQUIRED is on.
    """
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")

    from src.mcp_handlers.identity.handlers import handle_onboard_v2

    resolver = AsyncMock(side_effect=AssertionError("strict bare onboard must not mint"))
    with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolver):
        result = await handle_onboard_v2({})

    data = json.loads(result[0].text)
    assert data["success"] is True
    assert data["status"] == "lineage_declaration_required"
    assert data["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    assert "force_new=true" in data["hint"]
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_transport_injected_csid_onboard_refuses(monkeypatch):
    """A wrapper-injected CSID is transport context, not caller proof."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")

    from src.mcp_handlers.context import set_csid_transport_injected
    from src.mcp_handlers.identity.handlers import handle_onboard_v2

    resolver = AsyncMock(side_effect=AssertionError("injected CSID must not mint"))
    set_csid_transport_injected(True)
    try:
        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolver):
            result = await handle_onboard_v2({"client_session_id": "agent-missing"})
    finally:
        set_csid_transport_injected(False)

    data = json.loads(result[0].text)
    assert data["success"] is True
    assert data["status"] == "lineage_declaration_required"
    assert data["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    resolver.assert_not_awaited()
