"""REST pre_onboard reads resolve when the caller transmitted proof.

The MCP middleware short-circuits a pre_onboard read only when the call
carries no proof (#945 section 1). The REST prebind short-circuited every
pre_onboard read without a continuity token, so an agent passing its own
client_session_id could write its state over REST but read back 'unbound'
from check_working_state / get_governance_metrics. Found dogfooding
2026-09-24.

A server-inferred derivation (fingerprint, pin, transport-injected session
id) must still stay unbound: a read never mints and never shows a sibling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.http_api import _resolve_http_bound_agent

AGENT_UUID = "eb9420ca-6256-460d-bdb6-cf8ed90e1931"
SESSION_KEY = "agent-eb9420ca-625"


def _no_consult():
    return SimpleNamespace(binding=None, cacheable=False, transport_key=None)


def _derive_with(proof_origin: str):
    async def _derive(signals, arguments, *args, **kwargs):
        from src.mcp_handlers.context import set_session_proof_origin

        set_session_proof_origin(proof_origin)
        return SESSION_KEY

    return _derive


def _patches(proof_origin: str, resolve):
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    return [
        patch(
            "src.mcp_handlers.identity.operator.resolve_operator_identity",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.mcp_handlers.middleware.identity_step.consult_sticky_binding",
            AsyncMock(return_value=_no_consult()),
        ),
        patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            _derive_with(proof_origin),
        ),
        patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve),
        patch("src.db.get_db", MagicMock(return_value=db)),
    ]


async def _bind(tool_name: str, proof_origin: str, arguments=None):
    resolve = AsyncMock(
        return_value={"agent_uuid": AGENT_UUID, "created": False, "source": "session_binding"}
    )
    args = dict(arguments or {"client_session_id": SESSION_KEY})
    for p in _patches(proof_origin, resolve):
        p.start()
    try:
        bound = await _resolve_http_bound_agent(tool_name, args, None)
    finally:
        patch.stopall()
    _bind.last_arguments = args
    return bound, resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["check_working_state", "get_governance_metrics"])
async def test_caller_asserted_read_resolves_its_own_identity(tool_name):
    bound, resolve = await _bind(tool_name, "caller_asserted")

    assert bound == AGENT_UUID
    assert resolve.await_count == 1
    # Read-only resolution: never persists or mints.
    assert resolve.await_args.kwargs["persist"] is False
    assert resolve.await_args.kwargs["resume"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["check_working_state", "get_governance_metrics"])
async def test_server_inferred_read_stays_unbound(tool_name):
    bound, resolve = await _bind(tool_name, "server_inferred")

    assert bound is None
    assert resolve.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, arguments",
    [
        ("search_knowledge_graph", {"client_session_id": SESSION_KEY, "query": "backup"}),
        ("knowledge", {"client_session_id": SESSION_KEY, "action": "search", "query": "backup"}),
        ("check_working_state", {"client_session_id": SESSION_KEY}),
    ],
)
async def test_proof_read_stamps_no_target_and_leaves_no_cache(tool_name, arguments):
    """The caller is known from context; the arguments are left as sent, so a
    knowledge search is not narrowed to the caller's own findings."""
    cache = MagicMock()
    with patch("src.http_routes.access._cache_http_resolution", cache), \
         patch("src.http_routes.access._touch_http_session_activity", AsyncMock()) as touch:
        bound, _resolve = await _bind(tool_name, "caller_asserted", arguments)

    assert bound == AGENT_UUID
    assert "agent_id" not in _bind.last_arguments
    cache.assert_not_called()
    touch.assert_not_awaited()
