"""Tests for the one-call review flow and whose_move guidance.

Adoption UX (2026-07-28, amended 2026-08-23): request_review reuses a lone
issue description as the thesis; raw dialectic(action='request') retains the
explicit two-call flow. Non-terminal reads carry a plain-language `whose_move`
and `next_call`, including a no-copy way to reuse the saved brief.
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
    async def test_friendly_alias_defaults_lone_brief_to_thesis(self):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        arguments = {"issue_description": "Review the attached evidence"}
        ctx = DispatchContext()

        canonical, resolved, out_ctx = await resolve_alias(
            "request_review", arguments, ctx
        )

        assert canonical == "dialectic"
        assert resolved["use_brief_as_thesis"] is True
        assert out_ctx.normalized_parameters["use_brief_as_thesis"] == {
            "from": "omitted",
            "to": True,
            "interpretation": "alias_default",
        }

    @pytest.mark.parametrize(
        "arguments",
        [
            {"issue_description": "Neutral review", "use_brief_as_thesis": False},
            {"issue_description": "Review", "reasoning": "My explicit position"},
            {"issue_description": "Review", "root_cause": "Explicit cause"},
        ],
    )
    @pytest.mark.asyncio
    async def test_friendly_alias_preserves_explicit_opt_out(self, arguments):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        before = dict(arguments)
        ctx = DispatchContext()

        _canonical, resolved, out_ctx = await resolve_alias(
            "request_review", arguments, ctx
        )

        assert resolved["use_brief_as_thesis"] is before.get(
            "use_brief_as_thesis", True
        )
        if "use_brief_as_thesis" in before:
            assert out_ctx.normalized_parameters is None
        else:
            assert out_ctx.normalized_parameters["use_brief_as_thesis"]["to"] is True

    @pytest.mark.asyncio
    async def test_saved_brief_flag_triggers_thesis_without_copying(self, request_env):
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        brief = "Review this position, its evidence, and its open questions"
        with patch(
            f"{DIALECTIC}.handle_submit_thesis",
            new_callable=AsyncMock,
            return_value=_thesis_response({"success": True, "phase": "antithesis"}),
        ) as thesis:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "issue_description": brief,
                "use_brief_as_thesis": True,
            })

        data = parse_result(result)
        submitted = thesis.await_args.args[0]
        assert data["one_call_review"] is True
        assert data["thesis_source"] == "issue_description"
        assert submitted["root_cause"] == brief
        assert submitted["reasoning"] == brief

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
        """Raw requests without an opt-in retain the two-call protocol."""
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
        assert data["whose_move"].startswith("YOURS")
        assert "use_brief_as_thesis=true" in data["next_call"]
        thesis.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_session_reuses_saved_brief_for_thesis(self):
        from src.dialectic_protocol import DialecticSession
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis

        brief = "Saved review brief with the position and supporting evidence"
        session = DialecticSession(
            paused_agent_id="agent-paused",
            reviewer_agent_id="agent-reviewer",
            topic=brief,
            reason=brief,
        )
        with (
            patch(
                f"{DIALECTIC}._resolve_dialectic_agent_id",
                new_callable=AsyncMock,
                return_value=("agent-paused", None),
            ),
            patch(
                f"{DIALECTIC}.load_session",
                new_callable=AsyncMock,
                return_value=session,
            ),
            patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock) as add_message,
            patch(
                f"{DIALECTIC}.beam_update_phase",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(f"{DIALECTIC}._emit_phase_changed", new_callable=AsyncMock),
            patch(f"{DIALECTIC}.save_session", new_callable=AsyncMock),
        ):
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "use_brief_as_thesis": True,
            })

        data = parse_result(result)
        assert data["success"] is True, data
        assert data["thesis_source"] == "saved_session_brief"
        assert session.transcript[0].root_cause == brief
        assert session.transcript[0].reasoning == brief
        assert add_message.await_args.kwargs["root_cause"] == brief
        assert add_message.await_args.kwargs["reasoning"] == brief

    def test_recovery_session_still_requires_a_proposed_condition(self):
        from src.dialectic_protocol import DialecticMessage, DialecticSession

        session = DialecticSession(
            paused_agent_id="agent-paused",
            reviewer_agent_id="agent-reviewer",
            session_type="recovery",
        )
        result = session.submit_thesis(
            DialecticMessage(
                phase="thesis",
                agent_id="agent-paused",
                timestamp="2026-08-23T00:00:00+00:00",
                root_cause="Circuit breaker triggered",
                proposed_conditions=[],
                reasoning="Recovery needs an explicit safety condition",
            )
        )

        assert result["success"] is False
        assert "proposed_condition for recovery" in result["error"]


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

    def _operator(self, allowed=True):
        return patch(
            "src.mcp_handlers.identity.operator.is_operator_caller",
            return_value=allowed,
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

    def test_self_review_awaiting_ratification_stops_telling_the_agent_to_negotiate(self):
        """A refused self-review must not read as "your move" (#1585 item 1).

        Without this the paused agent — who is also the reviewer — is told to
        "negotiate until convergence" against itself, and every attempt is
        refused by the authority guard. The move belongs to an operator.
        """
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        session_data = {
            "session_id": "sess-self",
            "paused_agent_id": "agent-paused",
            "reviewer_agent_id": "agent-paused",
            "phase": "synthesis",
            "awaiting_facilitation": True,
        }

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability(dict(session_data))
        assert out["whose_move"].startswith("NOT YOURS")
        assert out["next_call"] is None
        assert out["required_role"] == "reviewer_or_operator"

        with self._ctx("an-operator"), self._operator():
            out = _build_dialectic_actionability(dict(session_data))
        assert out["whose_move"].startswith("YOURS")
        assert "action='reassign'" in out["next_call"]

    def test_self_review_without_the_refusal_still_negotiates(self):
        """The new branch keys on the routed state, not on self-review as such."""
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-self",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-paused",
                "phase": "synthesis",
            })
        assert out["whose_move"].startswith("YOURS")

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

    def test_standing_rejection_invites_one_paused_agent_response(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-rejected",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "phase": "synthesis",
                    "agent_id": "agent-reviewer",
                    "agrees": False,
                }],
            })
        assert out["whose_move"].startswith("YOURS")
        assert "action='synthesis'" in out["next_call"]
        assert out["current_agent_can_submit"] is True
        assert out["required_role"] == "paused_agent"
        assert "respond once" in out["recommended_action"]
        assert "independently ratifies" in out["recommended_action"]

    def test_standing_rejection_tells_observer_paused_response_is_owed(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("operator-agent"):
            out = _build_dialectic_actionability({
                "session_id": "sess-rejected",
                "paused_agent": "agent-paused",
                "reviewer": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "role": "synthesis",
                    "agent_id": "agent-reviewer",
                    "agrees": "false",
                }],
            })
        assert "paused agent" in out["whose_move"]
        assert out["next_call"] is None

    def test_standing_rejection_gives_credentialed_operator_a_reassignment_call(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("operator-agent"), self._operator():
            out = _build_dialectic_actionability({
                "session_id": "sess-rejected",
                "paused_agent": "agent-paused",
                "reviewer": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "role": "synthesis",
                    "agent_id": "agent-reviewer",
                    "agrees": "false",
                }, {
                    "role": "synthesis",
                    "agent_id": "agent-paused",
                    "agrees": "true",
                    "proposed_conditions": ["revised term"],
                }],
            })
        assert out["whose_move"].startswith("YOURS")
        assert "action='reassign'" in out["next_call"]
        assert "sess-rejected" in out["next_call"]

    def test_paused_agent_waits_while_reviewers_first_verdict_is_pending(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-pending",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "phase": "antithesis",
                    "agent_id": "agent-reviewer",
                }],
            })
        assert out["reviewer_verdict_pending"] is True
        assert out["whose_move"].startswith("NOT YOURS")
        assert out["next_call"] is None
        assert out["current_agent_can_submit"] is False

    def test_reviewer_is_prompted_for_first_pending_verdict(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-reviewer"):
            out = _build_dialectic_actionability({
                "session_id": "sess-pending",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "phase": "antithesis",
                    "agent_id": "agent-reviewer",
                }],
            })
        assert out["whose_move"].startswith("YOURS")
        assert "first synthesis verdict" in out["whose_move"]
        assert "action='synthesis'" in out["next_call"]

    def test_reviewer_may_revise_a_standing_rejection(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-reviewer"):
            out = _build_dialectic_actionability({
                "session_id": "sess-rejected",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "phase": "synthesis",
                    "agent_id": "agent-reviewer",
                    "agrees": False,
                }, {
                    "phase": "synthesis",
                    "agent_id": "agent-paused",
                    "agrees": True,
                    "proposed_conditions": ["revised term"],
                }],
            })
        assert out["whose_move"].startswith("YOURS")
        assert "maintain or revise" in out["whose_move"]
        assert "action='synthesis'" in out["next_call"]
        assert out["current_agent_can_submit"] is True

    def test_reviewer_waits_until_paused_agent_answers_rejection(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-reviewer"):
            out = _build_dialectic_actionability({
                "session_id": "sess-rejected",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "synthesis",
                "transcript": [{
                    "phase": "synthesis",
                    "agent_id": "agent-reviewer",
                    "agrees": False,
                }],
            })
        assert out["whose_move"].startswith("NOT YOURS")
        assert "paused agent" in out["whose_move"]
        assert out["next_call"] is None
        assert out["current_agent_can_submit"] is False

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

    def test_failed_session_with_facilitation_request_is_actionable(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("operator-agent"), self._operator():
            out = _build_dialectic_actionability({
                "session_id": "sess-failed-awaiting",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "failed",
                "awaiting_facilitation": True,
            })
        assert out["whose_move"].startswith("YOURS")
        assert "action='reassign'" in out["next_call"]
        assert out["required_role"] == "reviewer_or_operator"
        assert out["current_agent_can_submit"] is True

    def test_failed_session_without_facilitation_request_remains_terminal(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("operator-agent"), self._operator():
            out = _build_dialectic_actionability({
                "session_id": "sess-failed",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "failed",
                "awaiting_facilitation": False,
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
        assert "saved brief" in out["whose_move"]
        assert "use_brief_as_thesis=true" in out["next_call"]

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

    def test_recovery_actionability_keeps_required_condition_in_next_call(self):
        from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

        with self._ctx("agent-paused"):
            out = _build_dialectic_actionability({
                "session_id": "sess-recovery",
                "paused_agent_id": "agent-paused",
                "reviewer_agent_id": "agent-reviewer",
                "phase": "thesis",
                "session_type": "recovery",
            })

        assert "use_brief_as_thesis=true" in out["next_call"]
        assert "proposed_conditions=[...]" in out["next_call"]

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
