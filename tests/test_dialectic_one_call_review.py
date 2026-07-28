"""Tests for the one-call review flow and whose_move guidance.

Adoption UX (2026-07-28): request_review / dialectic(action='request') with
thesis-bearing fields (reasoning / root_cause) submits the thesis in the same
call; non-terminal session reads carry a plain-language `whose_move` +
`next_call` so a session waiting on the caller is never misread as a hang.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

DIALECTIC = "src.mcp_handlers.dialectic.handlers"


def parse_result(result):
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def clear_active_sessions():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield
    ACTIVE_SESSIONS.clear()


@pytest.fixture
def request_env():
    """Patch the request handler's collaborators for a clean session create."""
    meta = MagicMock()
    meta.status = "active"
    meta.tags = []
    server = MagicMock()
    server.agent_metadata = {"agent-paused": meta}
    server.load_metadata_async = AsyncMock()
    patches = [
        patch(f"{DIALECTIC}.mcp_server", server),
        patch(f"{DIALECTIC}.require_registered_agent", return_value=("agent-paused", None)),
        patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True),
        patch(f"{DIALECTIC}.pg_create_session", new_callable=AsyncMock),
        patch(
            f"{DIALECTIC}.is_agent_in_active_session",
            new_callable=AsyncMock, return_value=False,
        ),
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock, return_value=None),
    ]
    started = [p.start() for p in patches]
    yield started
    for p in patches:
        p.stop()


def _thesis_response(payload: dict):
    return [TextContent(type="text", text=json.dumps(payload))]


class TestOneCallReview:
    @pytest.mark.asyncio
    async def test_reasoning_triggers_thesis_in_same_call(self, request_env):
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with patch(
            f"{DIALECTIC}.handle_submit_thesis",
            new_callable=AsyncMock,
            return_value=_thesis_response({
                "success": True,
                "phase": "resolved",
                "resolution": {"action": "resume", "conditions": ["c1"]},
            }),
        ) as thesis:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "issue_description": "Review my decision to ship X",
                "reasoning": "I chose X because Y; my uncertainty is Z",
            })

        data = parse_result(result)
        assert data["one_call_review"] is True
        assert data["review_verdict"] == "resume"
        assert "session_id" in data
        assert data["whose_move"].startswith("nobody")
        # The thesis call received the mapped plain fields.
        kwargs = thesis.await_args.args[0]
        assert kwargs["root_cause"] == "Review my decision to ship X"
        assert kwargs["reasoning"] == "I chose X because Y; my uncertainty is Z"
        assert kwargs["session_id"] == data["session_id"]

    @pytest.mark.asyncio
    async def test_dispatched_reviewer_reports_whose_move(self, request_env):
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with patch(
            f"{DIALECTIC}.handle_submit_thesis",
            new_callable=AsyncMock,
            return_value=_thesis_response({
                "success": True,
                "phase": "antithesis",
                "reviewer_dispatch": {"agent_id": "ag-x", "via": "agent-orchestrator"},
            }),
        ):
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "issue_description": "Review this",
                "root_cause": "explicit root cause",
            })

        data = parse_result(result)
        assert data["one_call_review"] is True
        assert "reviewer" in data["whose_move"]
        assert "dialectic(action='get'" in data["whose_move"]

    @pytest.mark.asyncio
    async def test_without_thesis_fields_behavior_unchanged(self, request_env):
        """No reasoning/root_cause → today's two-call shape, thesis never runs."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with patch(
            f"{DIALECTIC}.handle_submit_thesis", new_callable=AsyncMock
        ) as thesis:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "Plain request",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert "one_call_review" not in data
        assert data["message"] == "Dialectic session created"
        thesis.assert_not_awaited()


class TestWhoseMove:
    def _ctx(self, agent):
        return patch(
            "src.mcp_handlers.context.get_context_agent_id", return_value=agent
        )

    def test_synthesis_owed_by_caller_reads_yours(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-1",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
            })
        assert out["whose_move"].startswith("YOURS")
        assert "action='synthesis'" in out["next_call"]
        assert "sess-1" in out["next_call"]

    def test_synthesis_for_observer_is_not_yours(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("someone-else"):
            out = _build_dialectic_actionability({
                "session_id": "sess-1",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
            })
        assert not out["whose_move"].startswith("YOURS")
        assert out["next_call"] is None

    def test_open_reviewer_slot_invites_claim(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("potential-reviewer"):
            out = _build_dialectic_actionability({
                "session_id": "sess-2",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": None,
                "phase": "antithesis",
            })
        assert "claim" in out["whose_move"]
        assert "action='antithesis'" in out["next_call"]

    def test_terminal_phase_owes_nothing(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx(None):
            out = _build_dialectic_actionability({
                "session_id": "sess-3",
                "paused_agent_id": "a",
                "reviewer_agent_id": "b",
                "phase": "resolved",
            })
        assert "nobody" in out["whose_move"]
        assert out["next_call"] is None
