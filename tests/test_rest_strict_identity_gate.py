"""#425 REST strict-identity gate — transport parity for the typed refusal.

Stage-1 burn-in (2026-06-11): under STRICT_IDENTITY_REQUIRED the MCP
dispatch middleware returned the designed typed refusal while the REST
surface (/v1/tools/call) skipped identity_step entirely — unbound reads
succeeded, unbound writes failed with an off-contract SESSION_ERROR.
The gate in http_tool_service.execute_http_tool mirrors the middleware
decision and shares its payload (identity_bootstrap.
strict_identity_refusal_payload) so the transports cannot drift.

Inert by default: the flag is off in every deployment until the #425
rollout advances.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
from src.services.http_tool_service import (
    _strict_identity_refusal_or_none,
    execute_http_tool,
)


@pytest.fixture
def strict_on(monkeypatch):
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")


@pytest.fixture
def strict_off(monkeypatch):
    monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)


@pytest.fixture
def unbound_context():
    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=None
    ):
        yield


# ---------------------------------------------------------------------------
# Payload (single source for both transports)
# ---------------------------------------------------------------------------


def test_refusal_payload_carries_the_425_contract_shape():
    p = strict_identity_refusal_payload("knowledge")
    assert p["status"] == "identity_required"
    assert p["tool"] == "knowledge"
    assert p["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    for key in ("hint", "ontology_ref", "tool_class"):
        assert p[key]


# ---------------------------------------------------------------------------
# Gate decision logic
# ---------------------------------------------------------------------------


def test_gate_inert_when_flag_off(strict_off, unbound_context):
    assert _strict_identity_refusal_or_none("knowledge", {}) is None


def test_gate_exempts_pre_onboard_tools(strict_on, unbound_context):
    # get_governance_metrics serves its own unbound shape (read purity);
    # onboard/identity ARE the identity-establishing tools.
    for tool in ("get_governance_metrics", "onboard", "identity", "health_check"):
        assert _strict_identity_refusal_or_none(tool, {}) is None


def test_gate_refuses_unbound_required_tool(strict_on, unbound_context):
    refusal = _strict_identity_refusal_or_none("knowledge", {})
    assert refusal == strict_identity_refusal_payload("knowledge")


def test_gate_fail_closed_for_unknown_tools(strict_on, unbound_context):
    """get_tool_identity_requirement defaults unknown names to 'required'
    — an unregistered/aliased name refuses rather than slipping through."""
    refusal = _strict_identity_refusal_or_none("no_such_tool_xyz", {})
    assert refusal is not None
    assert refusal["status"] == "identity_required"


@pytest.mark.parametrize(
    "args",
    [
        {"agent_id": "some-agent"},
        {"client_session_id": "agent-abc123"},
        {"continuity_token": "v1.someproof"},
    ],
)
def test_gate_passes_identity_bearing_arguments(strict_on, unbound_context, args):
    """Credential VALIDITY belongs to downstream resolution — a stale
    credential is an auth error, not an identity-required refusal."""
    assert _strict_identity_refusal_or_none("knowledge", args) is None


def test_gate_passes_context_bound_callers(strict_on):
    with patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="bound-agent",
    ):
        assert _strict_identity_refusal_or_none("knowledge", {}) is None


# ---------------------------------------------------------------------------
# execute_http_tool integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_refuses_before_fallback(strict_on, unbound_context):
    fallback = AsyncMock()
    with patch(
        "src.services.http_tool_service.execute_http_dispatch_fallback", fallback
    ), patch("src.services.http_tool_service.record_tool_usage"):
        result = await execute_http_tool("knowledge", {"action": "search", "query": "x"})

    assert result["status"] == "identity_required"
    assert result["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_refuses_before_beam_routing(strict_on, unbound_context):
    """The gate sits ahead of Wave-3a routing: a BEAM-routed handler never
    runs the Python identity middleware, so refusal must come first."""
    proxy = AsyncMock()
    with patch(
        "src.services.http_tool_service.wave3a_get_route",
        return_value="http://127.0.0.1:8770/fake",
    ), patch(
        "src.services.http_tool_service.proxy_to_beam", proxy
    ), patch("src.services.http_tool_service.record_tool_usage"):
        result = await execute_http_tool("knowledge", {})

    assert result["status"] == "identity_required"
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_flag_off_falls_through(strict_off, unbound_context):
    fallback = AsyncMock(return_value={"ok": True})
    with patch(
        "src.services.http_tool_service.execute_http_dispatch_fallback", fallback
    ), patch(
        "src.services.http_tool_service.wave3a_get_route", return_value=None
    ), patch("src.services.http_tool_service.record_tool_usage"):
        result = await execute_http_tool("knowledge", {"action": "search", "query": "x"})

    assert result == {"ok": True}
    fallback.assert_awaited_once()
