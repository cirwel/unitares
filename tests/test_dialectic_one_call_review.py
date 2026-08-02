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


# ----------------------------------------------------------------------
# #1414 regression locks.
#
# The one-call-review path launders identity through the public-handle slot:
# `require_registered_agent` rewrites arguments["agent_id"] to the PUBLIC
# HANDLE as a side effect, and the nested submit_thesis then re-resolves that
# handle against `core.identities`, which is keyed on the UUID. Result: the
# session row committed, the thesis was rejected as "not registered", and the
# response still said "thesis recorded".
#
# The `request_env` fixture above is why this shipped — it patches
# `require_registered_agent` with a plain return_value, so the handle-rewrite
# side effect never happened in test. These tests reproduce it.
# ----------------------------------------------------------------------

REAL_UUID = "3b531b97-a39d-4b95-aeb3-91a1003c9685"
REAL_HANDLE = "Claude_Opus_5_20260730"


def _rewrites_agent_id_to_handle(arguments):
    """Reproduce the real `require_registered_agent` mutation."""
    arguments["agent_id"] = REAL_HANDLE
    arguments["_agent_uuid"] = REAL_UUID
    return (REAL_UUID, None)


class TestOneCallIdentityForwarding:
    @pytest.fixture
    def env(self):
        meta = MagicMock()
        meta.status = "active"
        meta.tags = []
        server = MagicMock()
        server.agent_metadata = {REAL_UUID: meta}
        server.load_metadata_async = AsyncMock()
        patches = [
            patch(f"{DIALECTIC}.mcp_server", server),
            patch(
                f"{DIALECTIC}.require_registered_agent",
                side_effect=_rewrites_agent_id_to_handle,
            ),
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

    @pytest.mark.asyncio
    async def test_one_call_forwards_uuid_not_public_handle(self, env):
        """The tightest lock on the reported bug: the nested submit_thesis must
        receive the authoritative UUID, never the public handle."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with patch(
            f"{DIALECTIC}.handle_submit_thesis",
            new_callable=AsyncMock,
            return_value=_thesis_response({"success": True, "phase": "antithesis"}),
        ) as thesis:
            await handle_request_dialectic_review({
                "issue_description": "Review my decision to ship X",
                "reasoning": "I chose X because Y",
            })

        forwarded = thesis.await_args.args[0]
        assert forwarded["agent_id"] == REAL_UUID
        assert forwarded["agent_id"] != REAL_HANDLE

    @pytest.mark.asyncio
    async def test_failed_inline_thesis_does_not_claim_thesis_recorded(self, env):
        """A nested thesis failure must not be dressed as success. The session
        row is already committed, so the honest answer is 'the session exists,
        your thesis did not land, here is the retry call'."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with patch(
            f"{DIALECTIC}.handle_submit_thesis",
            new_callable=AsyncMock,
            return_value=_thesis_response({
                "success": False,
                "error": "Agent 'Claude_O...' is not registered",
            }),
        ):
            result = await handle_request_dialectic_review({
                "issue_description": "Review my decision to ship X",
                "reasoning": "I chose X because Y",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "thesis recorded" not in data["whose_move"]
        assert data["whose_move"].startswith("YOURS")
        assert data["thesis_recorded"] is False
        assert data["session_created"] is True
        assert data["session_id"]
        assert "action='thesis'" in data["next_call"]
        assert data["session_id"] in data["next_call"]

    @pytest.mark.asyncio
    async def test_issue_description_reaches_the_session_row(self, env):
        """Pydantic `model_dump()` materializes `reason` as an explicit None, so
        `arguments.get("reason", <default>)` returned None, not the default —
        and `issue_description` was never mapped onto reason/topic at all. The
        live orphan row was written with reason NULL and topic NULL."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        pg_create = env[3]
        with patch(
            f"{DIALECTIC}.handle_submit_thesis", new_callable=AsyncMock
        ):
            await handle_request_dialectic_review({
                "issue_description": "Trajectory-identity maths audit",
                "reason": None,          # the Pydantic model_dump() shape
                "topic": None,
            })

        kwargs = pg_create.await_args.kwargs
        assert kwargs["reason"] == "Trajectory-identity maths audit"
        assert kwargs["topic"] == "Trajectory-identity maths audit"


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

    # ------------------------------------------------------------------
    # #1414: two dict shapes reach this function. `load_session_as_dict`
    # (the default `get` fast path) emits "paused_agent"; only
    # `DialecticSession.to_dict` emits "paused_agent_id". The reviewer key
    # had a fallback and the paused key did not, so every fast-path read
    # reported allowed_agent_ids=[] and "Paused agent 'unassigned'" —
    # making a perfectly advanceable session look permanently stuck.
    # ------------------------------------------------------------------

    def test_actionability_reads_paused_agent_from_fast_path_dict(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-4",
                "paused_agent": "agent-paused",   # load_session_as_dict shape
                "reviewer": None,
                "phase": "thesis",
            })
        assert out["allowed_agent_ids"] == ["agent-paused"]
        assert out["required_agent_id"] == "agent-paused"
        assert "unassigned" not in out["recommended_action"]
        assert out["current_agent_role"] == "paused_agent"
        assert out["current_agent_can_submit"] is True

    def test_actionability_unchanged_for_to_dict_shape(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-5",
                "paused_agent_id": "agent-paused",   # to_dict shape
                "reviewer_agent_id": None,
                "phase": "thesis",
            })
        assert out["allowed_agent_ids"] == ["agent-paused"]
        assert out["required_agent_id"] == "agent-paused"
        assert "unassigned" not in out["recommended_action"]

    def test_actionability_treats_unknown_sentinel_as_absent(self):
        """`load_session_as_dict` coalesces a NULL paused_agent_id to the string
        "unknown". That sentinel must never land in allowed_agent_ids."""
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("unknown"):
            out = _build_dialectic_actionability({
                "session_id": "sess-6",
                "paused_agent": "unknown",
                "reviewer": None,
                "phase": "thesis",
            })
        assert out["allowed_agent_ids"] == []
        assert out["required_agent_id"] is None


class TestTimeoutInvariants:
    """#1442: every layer that runs the inline synthetic review must clear the
    budget of the work sharing its call. 55 < 60 was arithmetically fine and
    violated in practice — lock the ORDERING, not the constants, so retuning
    UNITARES_DIALECTIC_REVIEW_BUDGET can never silently reintroduce the drift.
    """

    def test_submit_thesis_clears_review_budget_plus_dispatch(self):
        from src.mcp_handlers.dialectic import handlers as H

        # Budget + orchestrated dispatch (≤10s) + fast-crash watch (≤20s):
        # the thesis call runs all three before the wrapper's wait_for fires.
        assert H.handle_submit_thesis._mcp_timeout >= (
            H._synthetic_review_budget() + 30.0
        )

    def test_one_call_request_clears_nested_submit_thesis(self):
        from src.mcp_handlers.dialectic import handlers as H

        # The one-call form invokes the DECORATED handle_submit_thesis (its own
        # wait_for included) after session creation, so the outer ceiling must
        # exceed the nested one — otherwise the outer kills the call while the
        # inner is still legitimately working (the #1442 failure).
        assert H.handle_request_dialectic_review._mcp_timeout >= (
            H.handle_submit_thesis._mcp_timeout + 10.0
        )

    def test_router_ceiling_clears_every_dialectic_action(self):
        from src.mcp_handlers.consolidated import handle_dialectic
        from src.mcp_handlers.dialectic import handlers as H

        # The consolidated `dialectic` router wraps each action handler in its
        # own wait_for; `request` is the slowest by construction (it embeds
        # submit_thesis). The router previously "cleared" submit_thesis's 90s
        # with timeout=90.0 — zero headroom, same drift shape.
        assert handle_dialectic._mcp_timeout > (
            H.handle_request_dialectic_review._mcp_timeout
        )
