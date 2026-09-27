"""``trigger_source`` records who started a review, and nothing a caller wrote.

It used to be guessed from substrings of the free-text reason. Both live
``loop_detection`` rows were agents that wrote "loop" in an ordinary review
request (56bead4ed32ab6a5, 755fe9368bb1c920), and the sustained-drift
trigger's reason matched "auto-triggered" first, so the one real
``drift_detection`` source was stored as ``circuit_breaker``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.dialectic.handlers import (
    AutomatedTrigger,
    _recorded_trigger_source,
    handle_request_dialectic_review,
)
from tests.helpers import parse_result
from tests.test_dialectic_handlers import (  # noqa: F401 — pytest fixtures
    DIALECTIC,
    mock_context_agent,
    mock_is_in_session,
    mock_pg_create,
    mock_require_registered,
    mock_server,
    mock_verify_ownership,
)


async def _request(fixtures, **arguments):
    server, require, ownership, pg_create, in_session, context = fixtures
    with require("agent-paused"), ownership, pg_create as created, in_session, context:
        result = await handle_request_dialectic_review({
            "agent_id": "agent-paused",
            "_agent_uuid": "agent-paused",
            "reviewer_mode": "self",
            **arguments,
        })
    assert parse_result(result)["success"] is True
    return created.await_args.kwargs["trigger_source"]


@pytest.fixture
def request_fixtures(
    mock_server, mock_require_registered, mock_verify_ownership,
    mock_pg_create, mock_is_in_session, mock_context_agent,
):
    return (mock_server, mock_require_registered, mock_verify_ownership,
            mock_pg_create, mock_is_in_session, mock_context_agent)


class TestATopicCannotSetTheSource:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", [
        "Exploration - probing whether UNITARES' self-governance loop produces useful insights",
        "PR #2124 closing #2123: the envelope rebuilt responses in a loop",
        "Auto-triggered by nothing at all; I typed this",
        "auto drift, but a person asked",
    ])
    async def test_free_text_is_recorded_as_manual(self, request_fixtures, reason):
        assert await _request(request_fixtures, reason=reason) == "manual"

    @pytest.mark.asyncio
    async def test_a_caller_cannot_claim_to_be_an_automated_trigger(self, request_fixtures):
        """The params middleware passes unknown keys through, so this plain
        string is exactly what an MCP caller's JSON would deliver."""
        assert await _request(request_fixtures, trigger_source="drift_detection") == "manual"


class TestAutomatedTriggersNameThemselves:
    @pytest.mark.asyncio
    async def test_an_in_process_trigger_is_recorded(self, request_fixtures):
        assert await _request(
            request_fixtures, reason="anything",
            trigger_source=AutomatedTrigger("drift_detection"),
        ) == "drift_detection"

    def test_an_empty_automated_trigger_is_still_manual(self):
        assert _recorded_trigger_source({"trigger_source": AutomatedTrigger("")}) == "manual"

    @pytest.mark.asyncio
    async def test_the_llm_path_receives_the_trigger_unchanged(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_is_in_session, mock_context_agent,
    ):
        llm = AsyncMock(return_value=[])
        trigger = AutomatedTrigger("circuit_breaker")
        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_is_in_session, mock_context_agent, \
             patch(f"{DIALECTIC}.handle_llm_assisted_dialectic", llm):
            await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "Auto-recovery: loop",
                "reviewer_mode": "llm",
                "trigger_source": trigger,
            })
        forwarded = llm.await_args.args[0]["trigger_source"]
        assert forwarded is trigger
        assert _recorded_trigger_source(llm.await_args.args[0]) == "circuit_breaker"

    @pytest.mark.asyncio
    async def test_sustained_drift_is_recorded_as_drift_detection(self):
        """Labels only. The trigger itself is unreachable in production: its
        ``from ..dialectic import is_agent_in_active_session`` has failed
        since the 2026-03-09 package reorg (131b83a08), and the ImportError is
        swallowed at debug level. Reviving an automatic review trigger is a
        policy decision, so this test supplies the missing name (create=True)
        rather than the fix."""
        from src.mcp_handlers.updates import phases

        monitor = SimpleNamespace(
            _consecutive_high_drift=3,
            _last_drift_vector=SimpleNamespace(norm=0.9),
            state=SimpleNamespace(),
        )
        ctx = SimpleNamespace(
            agent_id="agent-drifting",
            monitor=monitor,
            metrics_dict={},
            result={},
            arguments={},
            coherence=0.5,
            risk_score=0.0,
            previous_void_active=False,
        )
        request = AsyncMock(return_value=[])
        with patch("src.mcp_handlers.dialectic.is_agent_in_active_session",
                   new=AsyncMock(return_value=False), create=True), \
             patch(f"{DIALECTIC}.handle_request_dialectic_review", request):
            await phases._post_update_cirs_and_drift(ctx)

        request.assert_awaited_once()
        supplied = request.await_args.args[0]["trigger_source"]
        assert isinstance(supplied, AutomatedTrigger)
        assert _recorded_trigger_source(request.await_args.args[0]) == "drift_detection"
