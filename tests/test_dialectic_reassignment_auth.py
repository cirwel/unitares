"""Authorization boundaries for reviewer reassignment."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dialectic_protocol import DialecticPhase, DialecticSession
from src.mcp_handlers.dialectic.handlers import (
    _reviewer_reassignment_actor,
    handle_reassign_reviewer,
)
from tests.helpers import parse_result


def _session(*, self_review: bool = False) -> DialecticSession:
    paused = "agent-paused"
    session = DialecticSession(
        paused_agent_id=paused,
        reviewer_agent_id=paused if self_review else "agent-reviewer",
    )
    session.phase = DialecticPhase.ANTITHESIS
    return session


def test_operator_credential_authorizes_reassignment():
    with patch(
        "src.mcp_handlers.identity.operator.is_operator_caller",
        return_value=True,
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="operator-agent",
    ):
        assert _reviewer_reassignment_actor(_session()) == "operator"


def test_current_independent_reviewer_may_hand_off_its_assignment():
    with patch(
        "src.mcp_handlers.identity.operator.is_operator_caller",
        return_value=False,
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="agent-reviewer",
    ):
        assert _reviewer_reassignment_actor(_session()) == "current_reviewer"


@pytest.mark.parametrize("caller", ["agent-paused", "unrelated-agent", None])
def test_normal_bound_identity_cannot_choose_a_replacement_reviewer(caller):
    with patch(
        "src.mcp_handlers.identity.operator.is_operator_caller",
        return_value=False,
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value=caller,
    ):
        assert _reviewer_reassignment_actor(_session()) is None


def test_self_reviewer_is_still_the_interested_paused_agent():
    with patch(
        "src.mcp_handlers.identity.operator.is_operator_caller",
        return_value=False,
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="agent-paused",
    ):
        assert _reviewer_reassignment_actor(_session(self_review=True)) is None


@pytest.mark.asyncio
async def test_handler_refuses_paused_agent_before_candidate_selection_or_writes():
    session = _session()
    server = MagicMock()
    server.agent_metadata = {}
    server.load_metadata_async = AsyncMock()

    with patch(
        "src.mcp_handlers.dialectic.handlers.mcp_server", server
    ), patch(
        "src.mcp_handlers.dialectic.handlers.ACTIVE_SESSIONS",
        {session.session_id: session},
    ), patch(
        "src.mcp_handlers.identity.operator.is_operator_caller",
        return_value=False,
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="agent-paused",
    ), patch(
        "src.mcp_handlers.dialectic.handlers.select_reviewer",
        new_callable=AsyncMock,
    ) as select_reviewer:
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-chosen-by-paused-party",
        }))

    assert result["success"] is False
    assert result["error_code"] == "REASSIGN_FORBIDDEN"
    assert session.reviewer_agent_id == "agent-reviewer"
    select_reviewer.assert_not_awaited()
