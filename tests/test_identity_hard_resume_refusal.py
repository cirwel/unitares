"""Dispatch must stop when identity resolution returns a hard refusal.

Regression coverage for #1579.  A substrate-anchored UUID resumed over HTTP
returns ``resume_failed`` from the resolver.  That result is a terminal
authorization decision, not a partially resolved identity that may be bound
and threaded into the target handler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.context import SessionSignals, set_session_context
from src.mcp_handlers.middleware.identity_step import resolve_identity
from src.services.tool_dispatch_service import run_tool_dispatch_pipeline


_SUBSTRATE_UUID = "ffffffff-1111-4222-8333-444444444444"
_SUBSTRATE_HTTP_REFUSAL = {
    "resume_failed": True,
    "error": "substrate_anchored_uuid_requires_uds",
    "agent_uuid": _SUBSTRATE_UUID,
    "message": (
        "This substrate-anchored identity cannot resume over HTTP. "
        "Connect via UNITARES_UDS_SOCKET."
    ),
}


@pytest.mark.asyncio
async def test_substrate_http_refusal_short_circuits_dispatch(monkeypatch):
    """A hard resolver refusal reaches no binding, cache, TTL, or handler path."""
    monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)

    signals = SessionSignals(
        transport="rest",
        ip_ua_fingerprint="127.0.0.1:test-agent",
    )
    resolve_mock = AsyncMock(return_value=dict(_SUBSTRATE_HTTP_REFUSAL))
    recover_mock = AsyncMock(return_value=_SUBSTRATE_UUID)
    cache_mock = MagicMock()
    context_spy = MagicMock(wraps=set_session_context)
    db = MagicMock()
    db.update_session_activity = AsyncMock()
    handler = AsyncMock(return_value=["handler-ran"])

    with (
        patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=signals,
        ),
        patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new=AsyncMock(return_value="agent-substrate-http"),
        ),
        patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            resolve_mock,
        ),
        patch(
            "src.mcp_handlers.middleware.identity_step._maybe_recover_via_x_agent_id",
            recover_mock,
        ),
        patch(
            "src.mcp_handlers.middleware.identity_step.update_transport_binding",
            cache_mock,
        ),
        patch(
            "src.mcp_handlers.context.set_session_context",
            context_spy,
        ),
        patch(
            "src.db.get_db",
            return_value=db,
        ),
        patch(
            "src.mcp_handlers.TOOL_HANDLERS",
            {"process_agent_update": handler},
        ),
    ):
        result = await run_tool_dispatch_pipeline(
            name="process_agent_update",
            arguments={"client_session_id": "agent-substrate-http"},
            pre_steps=[resolve_identity],
            post_steps=[],
        )

    resolve_mock.assert_awaited_once()
    recover_mock.assert_not_awaited()
    context_spy.assert_not_called()
    cache_mock.assert_not_called()
    db.update_session_activity.assert_not_awaited()
    handler.assert_not_awaited()

    payload = json.loads(result[0].text)
    assert payload["status"] == "identity_required"
    assert payload["surface_context"]["resume_rejected_reason"] == (
        "substrate_anchored_uuid_requires_uds"
    )
    assert payload["surface_context"]["handler_invoked"] is False
    assert "UNITARES_UDS_SOCKET" in payload["hint"]
