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


def test_gate_passes_explicit_agent_id(strict_on, unbound_context):
    """An explicit agent_id is a cross-agent/legacy-name reference —
    require_agent_id and downstream ownership checks own it."""
    assert _strict_identity_refusal_or_none(
        "knowledge", {"agent_id": "some-agent"}
    ) is None


@pytest.mark.parametrize(
    "args",
    [
        # The transport-injected synthetic — http_call_tool stamps this
        # shape into EVERY request before the gate runs. Council live
        # battery (PR #610): a presence-based bypass passed all real
        # traffic; the gate must key on the RESOLVED binding instead.
        {"client_session_id": "http:127.0.0.1:abc123def456"},
        # A garbage caller-supplied credential that resolved to nothing.
        {"client_session_id": "x"},
        # An unresolved continuity token — a VALID token would have
        # produced a context binding via _resolve_http_bound_agent
        # (PATH 2.8) before the gate runs.
        {"continuity_token": "v1.garbage"},
    ],
)
def test_gate_refuses_unresolved_credentials(strict_on, unbound_context, args):
    """Credential presence is NOT identity: a credential that resolved
    has a context binding by gate time; one that didn't is unbound."""
    refusal = _strict_identity_refusal_or_none("knowledge", args)
    assert refusal is not None
    assert refusal["status"] == "identity_required"


def test_gate_passes_context_bound_callers(strict_on):
    """The resolved binding is THE pass condition — valid session ids,
    valid tokens, and explicit UUIDs all land here via
    _resolve_http_bound_agent before the gate runs."""
    with patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="bound-agent",
    ):
        assert _strict_identity_refusal_or_none(
            "knowledge", {"client_session_id": "agent-bound-123"}
        ) is None


# ---------------------------------------------------------------------------
# execute_http_tool integration
# ---------------------------------------------------------------------------


def test_refusal_payload_overrides_for_path_b_and_c():
    """Path B (bare onboard) and Path C (unregistered check-in) consume
    the same helper with overrides — drift in ontology_ref/tool_class
    across emission points was a council finding."""
    path_b = strict_identity_refusal_payload(
        "onboard", status="lineage_declaration_required", hint="custom"
    )
    assert path_b["status"] == "lineage_declaration_required"
    assert path_b["hint"] == "custom"
    assert path_b["ontology_ref"]  # no longer the empty string
    assert path_b["tool_class"] == "required"
    path_c = strict_identity_refusal_payload("process_agent_update", hint="h")
    assert path_c["status"] == "identity_required"
    assert path_c["ontology_ref"]


@pytest.mark.asyncio
async def test_execute_refuses_with_transport_injected_session(strict_on, unbound_context):
    """THE council-refuted case, now pinned: http_call_tool injects a
    synthetic client_session_id into every REST request before
    execute_http_tool runs. The gate must still refuse when that
    synthetic credential resolved to no binding."""
    fallback = AsyncMock()
    with patch(
        "src.services.http_tool_service.execute_http_dispatch_fallback", fallback
    ), patch("src.services.http_tool_service.record_tool_usage"):
        result = await execute_http_tool(
            "knowledge",
            {
                "action": "search",
                "query": "x",
                "client_session_id": "http:127.0.0.1:9aaaaac6dead",
            },
        )

    assert result["status"] == "identity_required"
    fallback.assert_not_awaited()


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
