"""REST identity-boundary regressions for issue #1431."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.http_api import _execute_http_tool_in_context
from src.mcp_handlers.context import (
    get_context_agent_id,
    get_context_resolved_agent_id,
    reset_session_context,
    set_session_context,
    update_context_agent_id,
)
from src.mcp_handlers.middleware import DispatchContext, inject_identity
from src.services.http_tool_service import _strict_identity_refusal_or_none


FORGED_UUID = "11111111-2222-3333-4444-555555555555"
BOUND_UUID = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.asyncio
async def test_unverified_x_agent_id_never_seeds_bound_context(monkeypatch):
    """The raw REST header cannot become a binding or pass the write gate."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    request = MagicMock()
    request.headers = {"x-agent-id": FORGED_UUID}
    arguments = {
        "action": "store",
        "client_session_id": "http:unbound",
        "summary": "must not run",
    }

    async def inspect_boundary(_tool_name, current_arguments):
        return {
            "context_agent_id": get_context_agent_id(),
            "resolved_agent_id": get_context_resolved_agent_id(),
            "refusal": _strict_identity_refusal_or_none(
                "knowledge", current_arguments
            ),
        }

    with patch(
        "src.http_routes.access._build_http_session_signals", return_value=MagicMock()
    ), patch(
        "src.http_routes.access._resolve_http_bound_agent",
        new=AsyncMock(return_value=None),
    ), patch(
        "src.http_routes.tools.execute_http_tool",
        new=AsyncMock(side_effect=inspect_boundary),
    ):
        observed = await _execute_http_tool_in_context(
            request,
            "knowledge",
            arguments,
            arguments["client_session_id"],
        )

    assert observed["context_agent_id"] is None
    assert observed["resolved_agent_id"] is None
    assert observed["refusal"]["status"] == "identity_required"


@pytest.mark.asyncio
async def test_forged_header_is_refused_before_rest_fallback(monkeypatch):
    """Exercise the real REST gate after an unresolved header-bearing call."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    request = MagicMock()
    request.headers = {"x-agent-id": FORGED_UUID}
    arguments = {
        "action": "store",
        "client_session_id": "http:unbound",
        "summary": "must not run",
    }
    fallback = AsyncMock(return_value={"unexpected": True})

    with patch(
        "src.http_routes.access._build_http_session_signals", return_value=MagicMock()
    ), patch(
        "src.http_routes.access._resolve_http_bound_agent",
        new=AsyncMock(return_value=None),
    ), patch(
        "src.services.http_tool_service.execute_http_dispatch_fallback",
        fallback,
    ), patch("src.services.http_tool_service.record_tool_usage"):
        result = await _execute_http_tool_in_context(
            request,
            "knowledge",
            arguments,
            arguments["client_session_id"],
        )

    assert result["status"] == "identity_required"
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_verified_http_identity_remains_bound(monkeypatch):
    """Removing the raw-header path preserves resolver-owned bindings."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    request = MagicMock()
    request.headers = {"x-agent-id": FORGED_UUID}
    arguments = {
        "action": "store",
        "client_session_id": "agent-12345678-123",
        "summary": "verified write",
    }

    async def resolve(_tool_name, current_arguments, _signals):
        update_context_agent_id(BOUND_UUID)
        current_arguments["agent_id"] = BOUND_UUID
        return BOUND_UUID

    async def inspect_boundary(_tool_name, current_arguments):
        return {
            "context_agent_id": get_context_agent_id(),
            "resolved_agent_id": get_context_resolved_agent_id(),
            "refusal": _strict_identity_refusal_or_none(
                "knowledge", current_arguments
            ),
        }

    with patch(
        "src.http_routes.access._build_http_session_signals", return_value=MagicMock()
    ), patch(
        "src.http_routes.access._resolve_http_bound_agent",
        new=AsyncMock(side_effect=resolve),
    ), patch(
        "src.http_routes.tools.execute_http_tool",
        new=AsyncMock(side_effect=inspect_boundary),
    ):
        observed = await _execute_http_tool_in_context(
            request,
            "knowledge",
            arguments,
            arguments["client_session_id"],
        )

    assert observed["context_agent_id"] == BOUND_UUID
    assert observed["resolved_agent_id"] == BOUND_UUID
    assert observed["refusal"] is None


@pytest.mark.asyncio
async def test_unverified_context_value_is_not_injected_into_handler_arguments():
    """Fallback dispatch accepts only middleware/resolver-owned bindings."""
    token = set_session_context(agent_id=FORGED_UUID)
    try:
        result = await inject_identity(
            "process_agent_update", {}, DispatchContext(bound_agent_id=None)
        )
    finally:
        reset_session_context(token)

    assert not isinstance(result, list)
    _, arguments, _ = result
    assert "agent_id" not in arguments


def test_strict_gate_rejects_transport_seed_without_resolution(monkeypatch):
    """A raw context value cannot masquerade as a resolved REST binding."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    token = set_session_context(agent_id=FORGED_UUID)
    try:
        refusal = _strict_identity_refusal_or_none(
            "knowledge", {"action": "store", "summary": "must not run"}
        )
    finally:
        reset_session_context(token)

    assert refusal is not None
    assert refusal["status"] == "identity_required"
