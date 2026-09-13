"""A synthesis comes only from a caller bound as the identity it submits for.

A synthesis can converge a dialectic session and trigger resolution.
``handle_submit_synthesis`` checks that the named agent is a participant (the
paused agent or the reviewer); that allow-list says which identity is named,
not who is calling. Synthesis also requires the resolver-stamped caller to
equal the resolved agent.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AUTH = "src.mcp_handlers.dialectic.auth"
DIALECTIC = "src.mcp_handlers.dialectic.handlers"
PAUSED = "22222222-2222-4222-8222-222222222222"
REVIEWER = "33333333-3333-4333-8333-333333333333"


def _server():
    server = MagicMock()
    meta = MagicMock()
    meta.public_agent_id = None
    meta.structured_id = None
    meta.label = None
    server.agent_metadata = {PAUSED: meta, REVIEWER: meta}
    return server


def _session():
    session = MagicMock()
    session.session_id = "sess-bound"
    session.paused_agent_id = PAUSED
    session.reviewer_agent_id = REVIEWER
    return session


async def _submit(resolved_caller, agent_id=REVIEWER):
    from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

    load = AsyncMock(return_value=_session())
    with patch(f"{AUTH}.mcp_server", _server()), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=resolved_caller), \
         patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=resolved_caller), \
         patch(f"{DIALECTIC}.load_session", load), \
         patch(f"{DIALECTIC}.get_session_lock", new_callable=AsyncMock, return_value=asyncio.Lock()):
        result = await handle_submit_synthesis({
            "session_id": "sess-bound",
            "agent_id": agent_id,
            "agrees": True,
            "proposed_conditions": ["c"],
            "root_cause": "rc",
            "reasoning": "r",
        })
    return json.loads(result[0].text), load


@pytest.mark.asyncio
async def test_an_unbound_caller_naming_the_reviewer_is_refused_before_the_session_loads():
    payload, load = await _submit(resolved_caller=None)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_caller_bound_as_one_participant_cannot_submit_as_the_other():
    payload, load = await _submit(resolved_caller=PAUSED, agent_id=REVIEWER)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_caller_bound_as_the_reviewer_passes_the_caller_check():
    """Past the check the handler loads the session; what happens next is not this test's."""
    payload, load = await _submit(resolved_caller=REVIEWER, agent_id=REVIEWER)
    assert payload.get("error_code") != "AUTH_REQUIRED"
    load.assert_awaited()


def test_caller_is_bound_as_reads_the_resolver_stamped_slot():
    from src.mcp_handlers.dialectic.auth import caller_is_bound_as

    with patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=REVIEWER):
        assert caller_is_bound_as(REVIEWER) is False
    with patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=REVIEWER):
        assert caller_is_bound_as(REVIEWER) is True
        assert caller_is_bound_as(PAUSED) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis"])
async def test_thesis_and_antithesis_require_the_same_bound_caller(action):
    """The session-ownership check only runs when a binding exists."""
    from src.mcp_handlers.dialectic import handlers

    handler = {
        "thesis": handlers.handle_submit_thesis,
        "antithesis": handlers.handle_submit_antithesis,
    }[action]
    load = AsyncMock(return_value=_session())
    named = PAUSED if action == "thesis" else REVIEWER
    with patch(f"{AUTH}.mcp_server", _server()), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None), \
         patch(f"{DIALECTIC}.load_session", load):
        result = await handler({
            "session_id": "sess-bound",
            "agent_id": named,
            "root_cause": "rc",
            "proposed_conditions": ["c"],
            "reasoning": "r",
            "concerns": ["x"],
        })
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert payload.get("error_code") == "AUTH_REQUIRED"
    load.assert_not_awaited()
