"""Tests for the end-to-end synthetic-reviewer completion in submit_thesis.

When a dialectic session has no independent live reviewer (the default, since
auto-select is disabled), submit_thesis drives the session to a resolved
synthesis via the local synthetic reviewer instead of stranding it at
awaiting_facilitation. These tests pin that completion, the multi-agent
preservation (a real reviewer is never pre-empted), and the env opt-out.
"""

import asyncio
import json

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.dialectic_protocol import DialecticSession, DialecticPhase
from tests.helpers import parse_result, successful_dialectic_message_append

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
LLM = "src.mcp_handlers.support.llm_delegation"


def _make_session(reviewer_id=None, phase=DialecticPhase.THESIS):
    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id=reviewer_id,
        session_type="recovery",
        paused_agent_state={
            "pause_effect_required": False,
            "pause_token": None,
        },
    )
    session.phase = phase
    return session


def _antithesis(position="dispute"):
    return {
        "concerns": ["risk_score alone ignores trajectory", "removes an audit checkpoint"],
        "counter_reasoning": "Low instantaneous risk is not a safe trajectory.",
        "grounding_cited": "coherence 0.38 + entropy 0.6",
        "position": position,
        "suggested_conditions": ["gate on coherence > 0.85"],
        "_structured": True,
    }


def _synthesis(rec="RESUME"):
    return {
        "agreed_root_cause": "Attention-management failure compounded by missing checkpoint",
        "reasoning": "Integrates the reviewer's checkpoint concern with the agent's plan.",
        "merged_conditions": ["Re-read before each edit", "Mandatory checkpoint after a failed edit"],
        "recommendation": rec,
        "_structured": True,
    }


@pytest.fixture
def server_patch():
    server = MagicMock()
    server.agent_metadata = {}   # .get(uuid) -> None -> api_key fallback
    server.monitors = {}         # no live monitor -> agent_state None
    with patch(f"{DIALECTIC}.mcp_server", server):
        yield server


@pytest.fixture(autouse=True)
def clear_sessions():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield
    ACTIVE_SESSIONS.clear()


@pytest.fixture(autouse=True)
def authoritative_pause_binding():
    """Bind one-shot LLM reviews to a deterministic durable pause."""
    with patch(
        "src.agent_storage.read_authoritative_pause_binding",
        new=AsyncMock(
            return_value={
                "pause_effect_required": True,
                "pause_token": "pause-1",
                "status": "paused",
            }
        ),
    ):
        yield


def _common_patches():
    """Patch the persistence + auth seams used by submit_thesis."""
    return [
        patch(f"{DIALECTIC}._resolve_dialectic_agent_id",
              new=AsyncMock(return_value=("agent-paused", None))),
        patch(f"{DIALECTIC}.load_session", new=AsyncMock(return_value=None)),
        patch(
            f"{DIALECTIC}.pg_add_message",
            new=AsyncMock(side_effect=successful_dialectic_message_append),
        ),
        patch(f"{DIALECTIC}.pg_update_phase", new=AsyncMock()),
        patch(f"{DIALECTIC}.beam_update_reviewer", new=AsyncMock(return_value=None)),
        patch(f"{DIALECTIC}.pg_update_reviewer", new=AsyncMock(return_value=True)),
        patch(f"{DIALECTIC}.pg_update_awaiting_facilitation", new=AsyncMock()),
        patch(f"{DIALECTIC}.pg_resolve_session", new=AsyncMock(return_value=True)),
        patch(
            f"{DIALECTIC}.execute_resolution",
            new=AsyncMock(
                return_value={"success": True, "applied_conditions": []}
            ),
        ),
        patch(f"{DIALECTIC}.record_resolution_outcome", new=AsyncMock()),
        patch(f"{DIALECTIC}.save_session", new=AsyncMock()),
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)),
    ]


@pytest.mark.asyncio
async def test_thesis_with_open_slot_resolves_via_synthetic_reviewer(server_patch):
    """No live reviewer -> submit_thesis runs antithesis+synthesis to RESOLVED.

    Resolve requires BOTH a RESUME recommendation AND a non-dispute antithesis,
    so the happy path uses a `refine` position (the reviewer accepts with edits).
    """
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis("refine"))))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
        # Gate OFF (default): the orchestrator must NOT be touched — pin no regression.
        no_dispatch = AsyncMock()
        stack.enter_context(patch("src.mcp_handlers.dialectic.orchestrator_dispatch.dispatch_orchestrated_review", new=no_dispatch))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit without re-reading the file",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(result)
    assert data["success"] is True
    assert data["synthetic_review"] is True
    assert "orchestrated_review" not in data
    no_dispatch.assert_not_called()
    assert data["reviewer_agent_id"] == "llm-synthetic-reviewer"
    assert data["recommendation"] == "RESUME"
    assert data["resolved"] is True
    assert data["antithesis"]["position"] == "refine"
    assert data["synthesis"]["merged_conditions"]
    assert session.phase == DialecticPhase.RESOLVED
    # The synthetic reviewer claimed the open slot.
    assert session.reviewer_agent_id == "llm-synthetic-reviewer"


@pytest.mark.asyncio
async def test_synthetic_reviewer_claims_open_slot_with_antithesis_append():
    """The DB authority guard must see the same reviewer as the protocol."""
    from src.mcp_handlers.dialectic.handlers import _run_synthetic_review

    proxy = "11111111-1111-4111-8111-111111111111"
    author = "22222222-2222-4222-8222-222222222222"
    session = DialecticSession(
        paused_agent_id=proxy,
        reviewer_agent_id=None,
        session_type="review",
        paused_agent_state={
                "pause_effect_required": False,
                "pause_token": None,
            "_dialectic_reviewed_work_authorship": {
                "schema": "unitares.dialectic-reviewed-work-authorship.v1",
                "status": "declared",
                "reviewed_work_author_agent_ids": [author],
                "declared_by_agent_id": proxy,
                "authorship_verified": False,
            }
        },
    )
    session.phase = DialecticPhase.ANTITHESIS
    beam_update_reviewer = AsyncMock(return_value=None)
    update_reviewer = AsyncMock(return_value=True)
    add_message = AsyncMock(side_effect=successful_dialectic_message_append)
    resolve_row = AsyncMock(return_value=True)

    with patch(f"{LLM}.local_endpoint_refusal", return_value=None), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.generate_antithesis",
        new=AsyncMock(return_value=_antithesis("refine")),
    ), patch(
        f"{LLM}.generate_synthesis",
        new=AsyncMock(return_value=_synthesis("RESUME")),
    ), patch(
        f"{DIALECTIC}.beam_update_reviewer", new=beam_update_reviewer
    ), patch(
        f"{DIALECTIC}.pg_update_reviewer", new=update_reviewer
    ), patch(
        f"{DIALECTIC}.pg_add_message", new=add_message,
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.beam_resolve", new=AsyncMock(return_value=None)
    ), patch(
        f"{DIALECTIC}.pg_resolve_session", new=resolve_row
    ), patch(
        f"{DIALECTIC}.execute_resolution",
        new=AsyncMock(return_value={"success": True, "applied_conditions": []}),
    ), patch(
        f"{DIALECTIC}.record_resolution_outcome", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}._emit_dialectic_event", new=AsyncMock()
    ):
        result = await _run_synthetic_review(
            session,
            {"root_cause": "rc", "proposed_conditions": ["c"]},
            proxy,
        )

    beam_update_reviewer.assert_not_awaited()
    update_reviewer.assert_not_awaited()
    antithesis_call = add_message.await_args_list[0]
    assert antithesis_call.kwargs["next_reviewer"] == "llm-synthetic-reviewer"
    assert antithesis_call.kwargs["expected_reviewer"] is None
    assert resolve_row.await_args.kwargs["expected_phase"] == "synthesis"
    assert (
        resolve_row.await_args.kwargs["expected_reviewer"]
        == "llm-synthetic-reviewer"
    )
    assert result["resolved"] is True


@pytest.mark.asyncio
async def test_synthetic_reviewer_aborts_when_open_slot_cas_loses():
    """A peer that claims first keeps the slot and receives no synthetic rows."""
    from src.mcp_handlers.dialectic.handlers import _run_synthetic_review

    session = _make_session(reviewer_id=None, phase=DialecticPhase.ANTITHESIS)
    session.paused_agent_state = {
        "pause_effect_required": True,
        "pause_token": "pause-1",
    }
    beam_claim = AsyncMock(return_value=None)
    claim = AsyncMock(return_value=False)
    add_message = AsyncMock(return_value=0)
    generate_synthesis = AsyncMock(return_value=_synthesis("RESUME"))

    with patch(f"{LLM}.local_endpoint_refusal", return_value=None), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.generate_antithesis",
        new=AsyncMock(return_value=_antithesis("refine")),
    ), patch(
        f"{LLM}.generate_synthesis", new=generate_synthesis
    ), patch(
        f"{DIALECTIC}.beam_update_reviewer", new=beam_claim
    ), patch(
        f"{DIALECTIC}.pg_update_reviewer", new=claim
    ), patch(
        f"{DIALECTIC}.pg_add_message", new=add_message
    ):
        result = await _run_synthetic_review(
            session,
            {"root_cause": "rc", "proposed_conditions": ["c"]},
            "agent-paused",
        )

    assert result is None
    beam_claim.assert_not_awaited()
    claim.assert_not_awaited()
    add_message.assert_awaited_once()
    assert add_message.await_args.kwargs["next_reviewer"] == (
        "llm-synthetic-reviewer"
    )
    generate_synthesis.assert_not_awaited()
    assert session.reviewer_agent_id is None
    assert session.phase == DialecticPhase.ANTITHESIS


@pytest.mark.asyncio
async def test_aborted_synthetic_claim_is_routed_to_facilitation(server_patch):
    """A claimed synthetic slot must not look like a resident reviewer."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    facilitation = AsyncMock()

    async def _abort_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise RuntimeError("transcript write failed")

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch(
                f"{DIALECTIC}.pg_mark_awaiting_facilitation",
                new=facilitation,
            )
        )
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_abort_after_claim)
        )
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is True
    assert data["reviewer_agent_id"] == SYNTHETIC_REVIEWER_ID
    assert data["awaiting_facilitation"] is True
    assert "claimed the session but did not finish" in data["note"]
    assert session.awaiting_facilitation is True
    facilitation.assert_awaited_once_with(
        session.session_id,
        expected_phase="antithesis",
        expected_reviewer="llm-synthetic-reviewer",
        expected_transcript_revision=1,
        expected_resolution_saga_revision=0,
        exclude_inflight_saga=True,
        touch_updated_at=True,
    )


@pytest.mark.asyncio
async def test_timed_out_synthetic_claim_is_guarded_into_facilitation(server_patch):
    """Timeout recovery flags only the live phase and reviewer it re-read."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    mark = AsyncMock(return_value=True)
    emit = AsyncMock()

    async def _timeout_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise asyncio.TimeoutError

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_timeout_after_claim)
        )
        stack.enter_context(patch(
            f"{DIALECTIC}.load_session_as_dict",
            new=AsyncMock(return_value={
                "status": "active",
                "phase": "antithesis",
                "reviewer": SYNTHETIC_REVIEWER_ID,
                "resolution_effect_state": None,
            }),
        ))
        stack.enter_context(patch(
            f"{DIALECTIC}.pg_mark_awaiting_facilitation", new=mark
        ))
        stack.enter_context(patch(f"{DIALECTIC}._emit_dialectic_event", new=emit))
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is True
    assert data["reviewer_agent_id"] == SYNTHETIC_REVIEWER_ID
    assert data["awaiting_facilitation"] is True
    assert data["terminal_state_unknown"] is False
    mark.assert_awaited_once_with(
        session.session_id,
        expected_phase="antithesis",
        expected_reviewer=SYNTHETIC_REVIEWER_ID,
        expected_updated_at=None,
        expected_transcript_revision=0,
        expected_resolution_saga_revision=0,
        exclude_inflight_saga=True,
    )
    assert session.awaiting_facilitation is True
    assert any(
        call.args[0] == "dialectic_facilitation_needed"
        for call in emit.await_args_list
    )


@pytest.mark.asyncio
async def test_timed_out_synthetic_terminal_readback_is_not_flagged(server_patch):
    """A terminal winner routes its durable effect and never asks for review."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    mark = AsyncMock()

    async def _timeout_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise asyncio.TimeoutError

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_timeout_after_claim)
        )
        stack.enter_context(patch(
            f"{DIALECTIC}.load_session_as_dict",
            new=AsyncMock(return_value={
                "status": "resolved",
                "phase": "resolved",
                "reviewer": SYNTHETIC_REVIEWER_ID,
                "resolution_effect_state": "pending",
            }),
        ))
        stack.enter_context(patch(
            f"{DIALECTIC}.pg_mark_awaiting_facilitation", new=mark
        ))
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["terminal_committed"] is True
    assert data["effect_pending"] is True
    assert data["resolution_effect_state"] == "pending"
    assert data["awaiting_facilitation"] is False
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_synthetic_failed_readback_surfaces_terminal_block(server_patch):
    """A late hard-limit commit must not return the stale successful thesis."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    mark = AsyncMock()

    async def _timeout_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise asyncio.TimeoutError

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_timeout_after_claim)
        )
        stack.enter_context(patch(
            f"{DIALECTIC}.load_session_as_dict",
            new=AsyncMock(return_value={
                "status": "failed",
                "phase": "failed",
                "reviewer": SYNTHETIC_REVIEWER_ID,
                "resolution": {
                    "action": "block",
                    "reason": "Safety threshold exceeded",
                },
                "resolution_effect_state": None,
            }),
        ))
        stack.enter_context(patch(
            f"{DIALECTIC}.pg_mark_awaiting_facilitation", new=mark
        ))
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["phase"] == "failed"
    assert data["terminal_status"] == "failed"
    assert data["terminal_committed"] is True
    assert data["hard_limit_blocked"] is True
    assert data["hard_limit_violation"] == "Safety threshold exceeded"
    assert data["terminal_state_unknown"] is False
    assert data["awaiting_facilitation"] is False
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_synthetic_unknown_readback_is_not_success(server_patch):
    """No readback and no guarded facilitation write is an unknown outcome."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    mark = AsyncMock()

    async def _timeout_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise asyncio.TimeoutError

    import contextlib
    with contextlib.ExitStack() as stack:
        for item in _common_patches():
            stack.enter_context(item)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_timeout_after_claim)
        )
        stack.enter_context(patch(
            f"{DIALECTIC}.load_session_as_dict",
            new=AsyncMock(return_value=None),
        ))
        stack.enter_context(patch(
            f"{DIALECTIC}.pg_mark_awaiting_facilitation", new=mark
        ))
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["terminal_state_unknown"] is True
    assert data["terminal_committed"] is None
    assert data["awaiting_facilitation"] is False
    assert "Reload this session" in data["next_step"]
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_resolved_without_effect_record_is_not_success(server_patch):
    """A resolved row without an applied outbox is not a confirmed outcome."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    async def _timeout_after_claim(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        raise asyncio.TimeoutError

    import contextlib
    with contextlib.ExitStack() as stack:
        for item in _common_patches():
            stack.enter_context(item)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_timeout_after_claim)
        )
        stack.enter_context(patch(
            f"{DIALECTIC}.load_session_as_dict",
            new=AsyncMock(return_value={
                "status": "resolved",
                "phase": "resolved",
                "reviewer": SYNTHETIC_REVIEWER_ID,
                "resolution_effect_state": None,
            }),
        ))
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["terminal_committed"] is True
    assert data["terminal_status"] == "resolved"
    assert data["effect_pending"] is True


@pytest.mark.asyncio
async def test_synthetic_terminal_write_refusal_is_not_reported_as_resolved():
    """A false effect-owner write leaves synthesis open and emits no success."""
    from src.mcp_handlers.dialectic.handlers import _run_synthetic_review

    session = _make_session(reviewer_id=None, phase=DialecticPhase.ANTITHESIS)
    emit = AsyncMock()

    with patch(f"{LLM}.local_endpoint_refusal", return_value=None), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.generate_antithesis",
        new=AsyncMock(return_value=_antithesis("refine")),
    ), patch(
        f"{LLM}.generate_synthesis",
        new=AsyncMock(return_value=_synthesis("RESUME")),
    ), patch(
        f"{DIALECTIC}.beam_update_reviewer", new=AsyncMock(return_value=None)
    ), patch(
        f"{DIALECTIC}.pg_update_reviewer", new=AsyncMock(return_value=True)
    ), patch(
        f"{DIALECTIC}.pg_add_message",
        new=AsyncMock(side_effect=successful_dialectic_message_append),
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.pg_resolve_session", new=AsyncMock(return_value=False)
    ), patch(
        f"{DIALECTIC}._emit_dialectic_event", new=emit
    ):
        result = await _run_synthetic_review(
            session,
            {"root_cause": "rc", "proposed_conditions": ["c"]},
            "agent-paused",
        )

    assert result["resolved"] is False
    assert session.phase == DialecticPhase.SYNTHESIS
    assert session.resolution is None
    assert "dialectic_resolved" not in [
        call.args[0] for call in emit.await_args_list
    ]


@pytest.mark.asyncio
async def test_synthetic_ambiguous_commit_stops_before_python_writer_and_effect():
    """A lost BEAM ack leaves ownership unknown and opens no second writer."""
    from src.mcp_handlers.dialectic.handlers import _run_synthetic_review

    session = _make_session(reviewer_id=None, phase=DialecticPhase.ANTITHESIS)
    emit = AsyncMock()
    facilitation = AsyncMock()

    async def _unknown_beam(*_args, detail=None, **_kwargs):
        detail["commit_unknown"] = True
        return None

    python_writer = AsyncMock()
    claim_effect = AsyncMock()

    with patch(f"{LLM}.local_endpoint_refusal", return_value=None), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.generate_antithesis",
        new=AsyncMock(return_value=_antithesis("refine")),
    ), patch(
        f"{LLM}.generate_synthesis",
        new=AsyncMock(return_value=_synthesis("RESUME")),
    ), patch(
        f"{DIALECTIC}.beam_update_reviewer", new=AsyncMock(return_value=None)
    ), patch(
        f"{DIALECTIC}.pg_update_reviewer", new=AsyncMock(return_value=True)
    ), patch(
        f"{DIALECTIC}.pg_add_message",
        new=AsyncMock(side_effect=successful_dialectic_message_append),
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.beam_resolve", new=_unknown_beam
    ), patch(
        f"{DIALECTIC}.pg_resolve_session", new=python_writer
    ), patch(
        f"{DIALECTIC}.pg_claim_resolution_effect", new=claim_effect
    ), patch(
        f"{DIALECTIC}.pg_update_awaiting_facilitation", new=facilitation
    ), patch(
        f"{DIALECTIC}._emit_dialectic_event", new=emit
    ):
        result = await _run_synthetic_review(
            session,
            {"root_cause": "rc", "proposed_conditions": ["c"]},
            "agent-paused",
        )

    assert result["resolved"] is False
    assert result["terminal_committed"] is False
    assert result["commit_unknown"] is True
    assert result["effect_pending"] is True
    assert session.phase == DialecticPhase.SYNTHESIS
    assert session.resolution is None
    assert session.awaiting_facilitation is False
    python_writer.assert_not_awaited()
    claim_effect.assert_not_awaited()
    facilitation.assert_not_awaited()
    event_types = [call.args[0] for call in emit.await_args_list]
    assert "dialectic_resolved" not in event_types
    assert "dialectic_facilitation_needed" not in event_types


@pytest.mark.asyncio
async def test_submit_thesis_reports_ambiguous_synthetic_commit_as_unsuccessful(
    server_patch,
):
    """The public envelope cannot turn unknown terminal ownership into success."""
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        SYNTHETIC_REVIEWER_ID,
        handle_submit_thesis,
    )

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    async def _ambiguous_review(*_args, **_kwargs):
        session.reviewer_agent_id = SYNTHETIC_REVIEWER_ID
        session.phase = DialecticPhase.SYNTHESIS
        return {
            "antithesis": _antithesis("refine"),
            "synthesis": _synthesis("RESUME"),
            "recommendation": "RESUME",
            "resolved": False,
            "terminal_committed": False,
            "commit_unknown": True,
            "effect_pending": True,
            "hard_limit_blocked": False,
            "hard_limit_violation": None,
        }

    import contextlib
    with contextlib.ExitStack() as stack:
        for item in _common_patches():
            stack.enter_context(item)
        stack.enter_context(
            patch(f"{DIALECTIC}._run_synthetic_review", new=_ambiguous_review)
        )
        response = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["commit_unknown"] is True
    assert data["effect_pending"] is True
    assert data["resolved"] is False
    assert "reconcile_effect" in data["next_step"]


@pytest.mark.asyncio
async def test_llm_assisted_terminal_write_refusal_requires_facilitation():
    """The one-shot synthetic writer also treats PostgreSQL as effect owner."""
    from src.mcp_handlers.dialectic.handlers import handle_llm_assisted_dialectic

    server = MagicMock()
    server.agent_metadata = {"agent-paused": MagicMock(status="active")}
    server.monitors = {}
    emit = AsyncMock()
    update_facilitation = AsyncMock(return_value=True)
    graph = MagicMock()
    graph.add_discovery = AsyncMock()
    full_dialectic = {
        "success": True,
        "recommendation": "RESUME",
        "antithesis": _antithesis("refine"),
        "synthesis": _synthesis("RESUME"),
    }

    with patch(f"{DIALECTIC}.mcp_server", server), patch(
        f"{DIALECTIC}.require_registered_agent",
        return_value=("agent-paused", None),
    ), patch(
        f"{DIALECTIC}.resolve_agent_uuid", return_value="agent-paused"
    ), patch(
        f"{LLM}.local_endpoint_refusal", return_value=None
    ), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.run_full_dialectic", new=AsyncMock(return_value=full_dialectic)
    ), patch(
        f"{DIALECTIC}.pg_create_session", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.pg_add_message",
        new=AsyncMock(side_effect=successful_dialectic_message_append),
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.pg_mark_awaiting_facilitation",
        new=update_facilitation,
    ), patch(
        f"{DIALECTIC}.pg_resolve_session", new=AsyncMock(return_value=False)
    ), patch(
        f"{DIALECTIC}._emit_dialectic_event", new=emit
    ), patch(
        "src.knowledge_graph.get_knowledge_graph",
        new=AsyncMock(return_value=graph),
    ):
        response = await handle_llm_assisted_dialectic({
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before retrying"],
        })

    data = parse_result(response)
    assert data["success"] is False
    assert data["error_code"] == "SESSION_RESOLUTION_WRITE_REFUSED"
    assert data["terminal_committed"] is False
    update_facilitation.assert_awaited_once()
    event_types = [call.args[0] for call in emit.await_args_list]
    assert "dialectic_resolved" not in event_types
    assert "dialectic_facilitation_needed" in event_types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect,expected_resolved,expected_success",
    [
        (("applied", True), True, True),
        (("unknown_partial", True), False, False),
    ],
)
async def test_llm_assisted_consumes_durable_effect_before_reporting_resolution(
    effect, expected_resolved, expected_success
):
    from src.mcp_handlers.dialectic.handlers import (
        _ResolutionCommitResult,
        _ResolutionEffectResult,
        handle_llm_assisted_dialectic,
    )

    server = MagicMock()
    server.agent_metadata = {
        "agent-paused": MagicMock(status="paused", paused_at="pause-1")
    }
    server.monitors = {}
    graph = MagicMock()
    graph.add_discovery = AsyncMock()
    emit = AsyncMock()
    state, claimed = effect
    full_dialectic = {
        "success": True,
        "recommendation": "RESUME",
        "antithesis": _antithesis("refine"),
        "synthesis": _synthesis("RESUME"),
    }

    with patch(f"{DIALECTIC}.mcp_server", server), patch(
        f"{DIALECTIC}.require_registered_agent",
        return_value=("agent-paused", None),
    ), patch(
        f"{DIALECTIC}.resolve_agent_uuid", return_value="agent-paused"
    ), patch(
        f"{LLM}.local_endpoint_refusal", return_value=None
    ), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.run_full_dialectic", new=AsyncMock(return_value=full_dialectic)
    ), patch(
        f"{DIALECTIC}.pg_create_session", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.pg_add_message",
        new=AsyncMock(side_effect=successful_dialectic_message_append),
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}._commit_converged_resolution",
        new=AsyncMock(return_value=_ResolutionCommitResult(True, False, "test")),
    ), patch(
        f"{DIALECTIC}._apply_durable_resolution_effect",
        new=AsyncMock(return_value=_ResolutionEffectResult(
            state,
            claimed,
            {"success": state == "applied"},
            None if state == "applied" else "partial",
        )),
    ) as apply_effect, patch(
        f"{DIALECTIC}._emit_dialectic_event", new=emit
    ), patch(
        "src.knowledge_graph.get_knowledge_graph",
        new=AsyncMock(return_value=graph),
    ):
        response = await handle_llm_assisted_dialectic({
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before retrying"],
        })

    data = parse_result(response)
    assert data["terminal_committed"] is True
    assert data["resolved"] is expected_resolved
    assert data["success"] is expected_success
    assert data["resolution_effect_state"] == state
    if state == "unknown_partial":
        assert "runtime effect is not confirmed" in data["message"]
        assert all(
            "you can resume" not in step.lower() for step in data["next_steps"]
        )
        discovery = graph.add_discovery.await_args.args[0]
        assert "effect-pending" in discovery.tags
        assert "resume" not in discovery.tags
    apply_effect.assert_awaited_once()
    resolved_events = [
        call for call in emit.await_args_list if call.args[0] == "dialectic_resolved"
    ]
    assert len(resolved_events) == (1 if state == "applied" else 0)


async def _run_llm_assisted_truthfulness_case(*, hard_limits, commit):
    from src.mcp_handlers.dialectic.handlers import handle_llm_assisted_dialectic

    server = MagicMock()
    server.agent_metadata = {
        "agent-paused": MagicMock(status="paused", paused_at="pause-1")
    }
    server.monitors = {}
    graph = MagicMock()
    graph.add_discovery = AsyncMock()
    full_dialectic = {
        "success": True,
        "recommendation": "RESUME",
        "antithesis": _antithesis("refine"),
        "synthesis": _synthesis("RESUME"),
    }
    apply_effect = AsyncMock()

    with patch(f"{DIALECTIC}.mcp_server", server), patch(
        f"{DIALECTIC}.require_registered_agent",
        return_value=("agent-paused", None),
    ), patch(
        f"{DIALECTIC}.resolve_agent_uuid", return_value="agent-paused"
    ), patch(
        f"{LLM}.local_endpoint_refusal", return_value=None
    ), patch(
        f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)
    ), patch(
        f"{LLM}.run_full_dialectic", new=AsyncMock(return_value=full_dialectic)
    ), patch(
        f"{DIALECTIC}.pg_create_session", new=AsyncMock()
    ), patch(
        f"{DIALECTIC}.pg_add_message",
        new=AsyncMock(side_effect=successful_dialectic_message_append),
    ), patch(
        f"{DIALECTIC}.pg_update_phase", new=AsyncMock()
    ), patch.object(
        DialecticSession, "check_hard_limits", return_value=hard_limits
    ), patch(
        f"{DIALECTIC}._commit_converged_resolution",
        new=AsyncMock(return_value=commit),
    ), patch(
        f"{DIALECTIC}._commit_terminal_resolution",
        new=AsyncMock(return_value=commit),
    ), patch(
        f"{DIALECTIC}._apply_durable_resolution_effect", new=apply_effect
    ), patch(
        "src.knowledge_graph.get_knowledge_graph",
        new=AsyncMock(return_value=graph),
    ):
        response = await handle_llm_assisted_dialectic({
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before retrying"],
        })

    return parse_result(response), graph, apply_effect


@pytest.mark.asyncio
async def test_llm_assisted_hard_limit_guidance_never_authorizes_resume():
    from src.mcp_handlers.dialectic.handlers import _ResolutionCommitResult

    data, graph, apply_effect = await _run_llm_assisted_truthfulness_case(
        hard_limits=(False, "Safety threshold exceeded"),
        commit=_ResolutionCommitResult(True, False, "test"),
    )

    assert data["success"] is False
    assert data["hard_limit_blocked"] is True
    assert "terminally blocked" in data["message"]
    assert all("you can resume" not in step.lower() for step in data["next_steps"])
    apply_effect.assert_not_awaited()
    discovery = graph.add_discovery.await_args.args[0]
    assert "hard-limit-blocked" in discovery.tags
    assert "resume" not in discovery.tags
    assert json.loads(discovery.details)["hard_limit_blocked"] is True


@pytest.mark.asyncio
async def test_llm_assisted_commit_unknown_guidance_requires_reconciliation():
    from src.mcp_handlers.dialectic.handlers import _ResolutionCommitResult

    data, graph, apply_effect = await _run_llm_assisted_truthfulness_case(
        hard_limits=(True, None),
        commit=_ResolutionCommitResult(False, True, "beam_commit_unknown"),
    )

    assert data["success"] is False
    assert data["commit_unknown"] is True
    assert "ownership is unknown" in data["message"]
    assert any("reconcile" in step.lower() for step in data["next_steps"])
    assert all("you can resume" not in step.lower() for step in data["next_steps"])
    apply_effect.assert_not_awaited()
    discovery = graph.add_discovery.await_args.args[0]
    assert "terminal-commit-unknown" in discovery.tags
    assert "resume" not in discovery.tags
    assert json.loads(discovery.details)["commit_unknown"] is True


@pytest.mark.asyncio
async def test_disputed_thesis_does_not_auto_resolve_even_on_resume(server_patch):
    """The live-2026-06-23 rubber-stamp: a `position=dispute` antithesis with a
    RESUME synthesis must NOT auto-resolve. The dialectic work is recorded but the
    session falls through to facilitation rather than rubber-stamping a resume."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis("dispute"))))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "The pause is spurious noise; disable the risk check and resume.",
            "proposed_conditions": ["Resume with no conditions"],
            "reasoning": "There is no real risk.",
        })

    data = parse_result(result)
    assert data["success"] is True
    assert data["antithesis"]["position"] == "dispute"
    # Recommendation may still read RESUME, but it must NOT bind to a resolution.
    assert data["resolved"] is False
    assert session.phase != DialecticPhase.RESOLVED
    assert data["awaiting_facilitation"] is True
    assert session.awaiting_facilitation is True


OD = "src.mcp_handlers.dialectic.orchestrator_dispatch"


@pytest.mark.asyncio
async def test_orchestrated_dispatch_skips_in_process(server_patch):
    """Escalation tier (design b): when orchestrated review is enabled and the
    dispatch succeeds, the handler returns the dispatch result WITHOUT running the
    in-process synthetic reviewer (generate_antithesis must not be called)."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    session.topic = "Investigate false high-risk pause"
    session.reason = "Risk guard requested independent review"
    session.paused_agent_state = {"risk_score": 0.7859, "coherence": 0.4986}
    ACTIVE_SESSIONS[session.session_id] = session
    gen_anti = AsyncMock(return_value=_antithesis())
    dispatch = AsyncMock(return_value={"ok": True, "execution_id": "ex-review-1"})
    emit = AsyncMock()

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=gen_anti))
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review", new=dispatch))
        # reviewer is running/healthy (not a fast crash) → async path owns it
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=AsyncMock(return_value=False)))
        stack.enter_context(patch(f"{DIALECTIC}._emit_dialectic_event", new=emit))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert data["orchestrated_review"] is True
    assert data["reviewer_dispatch"]["execution_id"] == "ex-review-1"
    assert data["reviewer_dispatch"]["agent_id"] == "ex-review-1"
    # The in-process reviewer must NOT have run — the orchestrator owns this review.
    gen_anti.assert_not_called()
    assert "resolved" not in data  # slot left open for the spawned reviewer
    dispatched_thesis = dispatch.await_args.args[1]
    assert dispatched_thesis["paused_agent_state"] == {
        "risk_score": 0.7859,
        "coherence": 0.4986,
    }
    assert "Topic: Investigate false high-risk pause" in dispatched_thesis["situation"]
    assert "Reason: Risk guard requested independent review" in dispatched_thesis["situation"]
    assert "dialectic_reviewer_dispatched" in [
        call.args[0] for call in emit.await_args_list
    ]
    dispatch_event = next(
        call
        for call in emit.await_args_list
        if call.args[0] == "dialectic_reviewer_dispatched"
    )
    assert dispatch_event.kwargs["orchestrator_execution_id"] == "ex-review-1"
    assert dispatch_event.kwargs["orchestrator_agent_id"] == "ex-review-1"
    assert session.awaiting_facilitation is False


@pytest.mark.asyncio
async def test_orchestrated_dispatch_survives_reviewer_claiming_slot_first(server_patch):
    """The spawned reviewer can claim the slot during the fast-crash wait. Its
    antithesis moves the phase, reviewer and transcript revision, so a
    compare-and-set against the pre-dispatch snapshot is refused. The thesis is
    recorded and the reviewer owns the slot, so the caller must hear success,
    not ``state_reload_required`` (dialectic canary, 2026-10-18)."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    dispatch = AsyncMock(return_value={"ok": True, "execution_id": "ex-review-fast"})
    # What the database answers once the reviewer's antithesis has landed.
    refused = AsyncMock(return_value=False)
    emit = AsyncMock()

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch(f"{DIALECTIC}.pg_update_awaiting_facilitation", new=refused)
        )
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review", new=dispatch))
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=AsyncMock(return_value=False)))
        stack.enter_context(patch(f"{DIALECTIC}._emit_dialectic_event", new=emit))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert data["success"] is True
    assert "state_reload_required" not in data
    assert data["reviewer_dispatch"]["execution_id"] == "ex-review-fast"
    assert "dialectic_reviewer_dispatched" in [
        call.args[0] for call in emit.await_args_list
    ]
    refused.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrated_dispatch_clears_a_stale_flag_with_the_thesis(server_patch):
    """A session can arrive carrying a creation-time facilitation flag. The
    thesis append clears it atomically, so the dispatch path has nothing left to
    reconcile and must not attempt the snapshot compare-and-set that a fast
    reviewer would refuse."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    session.awaiting_facilitation = True
    ACTIVE_SESSIONS[session.session_id] = session
    append = AsyncMock(side_effect=successful_dialectic_message_append)
    refused = AsyncMock(return_value=False)
    dispatch = AsyncMock(return_value={"ok": True, "execution_id": "ex-review-stale"})

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{DIALECTIC}.pg_add_message", new=append))
        stack.enter_context(
            patch(f"{DIALECTIC}.pg_update_awaiting_facilitation", new=refused)
        )
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review", new=dispatch))
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=AsyncMock(return_value=False)))
        stack.enter_context(patch(f"{DIALECTIC}._emit_dialectic_event", new=AsyncMock()))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert data["success"] is True
    assert append.await_args.kwargs["next_awaiting_facilitation"] is False
    refused.assert_not_awaited()
    assert session.awaiting_facilitation is False


@pytest.mark.asyncio
async def test_orchestrated_reviewer_fast_crash_falls_back_to_in_process(server_patch):
    """Dispatch succeeds but the spawned reviewer crashes fast (exits non-zero
    before claiming the slot) → fall back to in-process inline so the session
    resolves now instead of stranding at antithesis for the 4h reap."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis("refine"))))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review",
                                  new=AsyncMock(return_value={"ok": True, "agent_id": "ag-crash"})))
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=AsyncMock(return_value=True)))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert "orchestrated_review" not in data
    # fell back inline: synthetic review ran and resolved (refine + RESUME)
    assert data["synthetic_review"] is True
    assert data["resolved"] is True


@pytest.mark.asyncio
async def test_orchestrated_dispatch_failure_falls_back_to_in_process(server_patch):
    """If dispatch returns None (orchestrator down / no bearer), the handler
    degrades to the in-process synthetic reviewer — dialectic still completes."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis("refine"))))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review", new=AsyncMock(return_value=None)))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert "orchestrated_review" not in data
    # Fell back to in-process: synthetic review ran and resolved (refine + RESUME).
    assert data["synthetic_review"] is True
    assert data["resolved"] is True


async def _append_refusing_the_synthetic_antithesis(*args, **kwargs):
    """The thesis append commits; the in-process antithesis append loses the
    open-slot CAS because the spawned reviewer's antithesis landed first."""
    if kwargs.get("message_type") == "antithesis":
        return 0
    return await successful_dialectic_message_append(*args, **kwargs)


# What the database holds once the spawned reviewer's antithesis has landed.
_REVIEWER_CLAIMED_ROW = {
    "session_id": "unused",
    "status": "active",
    "phase": "synthesis",
    "reviewer": "reviewer-orchestrated",
    "awaiting_facilitation": False,
    "transcript_revision": 2,
    "resolution_saga_revision": 0,
    "resolution_effect_state": None,
}


def _reviewer_claimed_session():
    """The re-read session once the spawned reviewer's antithesis landed."""
    fresh = _make_session(
        reviewer_id="reviewer-orchestrated", phase=DialecticPhase.SYNTHESIS
    )
    fresh.transcript_revision = 2
    return fresh


async def _submit_thesis_while_reviewer_claims_slot(
    *, dispatch, crashed_fast, readback, mark=None, fresh_session=None
):
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    append = AsyncMock(side_effect=_append_refusing_the_synthetic_antithesis)
    mark = mark or AsyncMock(return_value=False)
    emit = AsyncMock()

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{DIALECTIC}.pg_add_message", new=append))
        stack.enter_context(
            patch(f"{DIALECTIC}.pg_mark_awaiting_facilitation", new=mark)
        )
        stack.enter_context(
            patch(f"{DIALECTIC}.load_session_as_dict", new=AsyncMock(return_value=readback))
        )
        async def _load(_session_id):
            # The cached snapshot serves the thesis; only the re-read after the
            # refused antithesis append sees the reviewer's claim.
            refused = any(
                c.kwargs.get("message_type") == "antithesis"
                for c in append.await_args_list
            )
            return fresh_session if refused else None

        stack.enter_context(
            patch(f"{DIALECTIC}.load_session", new=AsyncMock(side_effect=_load))
        )
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis("refine"))))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review", new=dispatch))
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=crashed_fast))
        stack.enter_context(patch(f"{DIALECTIC}._emit_dialectic_event", new=emit))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    return session, parse_result(result), append, mark, emit


def _assert_recorded_thesis_with_reviewer_in_charge(session, data, append, mark, emit, dispatch):
    assert data["success"] is True, data
    assert data["thesis_recorded"] is True
    assert data["session_advanced"] is True
    assert data["reviewer_agent_id"] == "reviewer-orchestrated"
    assert data["phase"] == "synthesis"
    assert data["awaiting_facilitation"] is False
    assert "state_reload_required" not in data
    assert "synthetic_review" not in data
    assert "error" not in data
    # One dispatch, one thesis append, one refused antithesis attempt; the
    # in-process reviewer wrote nothing over the spawned reviewer's claim.
    dispatch.assert_awaited_once()
    assert [c.kwargs["message_type"] for c in append.await_args_list] == [
        "thesis", "antithesis",
    ]
    mark.assert_awaited_once()
    assert "dialectic_facilitation_needed" not in [
        c.args[0] for c in emit.await_args_list
    ]
    # The stale snapshot is evicted and left without a facilitation request.
    from src.mcp_handlers.dialectic.handlers import ACTIVE_SESSIONS
    assert session.session_id not in ACTIVE_SESSIONS
    assert session.awaiting_facilitation is False


@pytest.mark.asyncio
async def test_reviewer_that_claims_slot_then_crashes_fast_leaves_thesis_recorded(server_patch):
    """#2905, crash trigger: the spawned reviewer records its antithesis and
    then exits non-zero inside the fast-crash wait. The in-process fallback
    finds the slot taken and the facilitation compare-and-set is refused. The
    thesis and the reviewer's antithesis are both on record, so the caller
    must hear that, not 'thesis NOT recorded'. The reviewer that holds the
    slot has exited and will not act again, so the facilitation request is
    recorded against the re-read row instead of reporting it in charge."""
    dispatch = AsyncMock(return_value={"ok": True, "execution_id": "ex-claim-then-crash"})
    crashed_fast = AsyncMock(return_value=True)
    mark = AsyncMock(side_effect=[False, True])
    fresh = _reviewer_claimed_session()

    session, data, append, mark, emit = await _submit_thesis_while_reviewer_claims_slot(
        dispatch=dispatch, crashed_fast=crashed_fast, readback=dict(_REVIEWER_CLAIMED_ROW),
        mark=mark, fresh_session=fresh,
    )

    assert data["success"] is True, data
    assert data["thesis_recorded"] is True
    assert data["session_advanced"] is True
    assert data["reviewer_exited"] is True
    assert data["reviewer_agent_id"] == "reviewer-orchestrated"
    assert data["awaiting_facilitation"] is True
    assert "state_reload_required" not in data
    assert "no facilitation request was needed" not in data["note"]
    assert "replacement reviewer" in data["next_step"]
    crashed_fast.assert_awaited_once_with("ex-claim-then-crash")
    dispatch.assert_awaited_once()
    assert [c.kwargs["message_type"] for c in append.await_args_list] == [
        "thesis", "antithesis",
    ]
    # The stale snapshot's mark is refused; the re-read session's mark is
    # made against the reviewer that now holds the slot.
    assert mark.await_count == 2
    assert mark.await_args_list[1].kwargs["expected_reviewer"] == "reviewer-orchestrated"
    assert mark.await_args_list[1].kwargs["expected_phase"] == "synthesis"
    assert mark.await_args_list[1].kwargs["expected_transcript_revision"] == 2
    needed = [
        c for c in emit.await_args_list if c.args[0] == "dialectic_facilitation_needed"
    ]
    assert len(needed) == 1
    assert needed[0].kwargs["reason"] == "reviewer_crashed_fast"
    assert fresh.awaiting_facilitation is True


@pytest.mark.asyncio
async def test_exited_reviewer_whose_session_moves_again_needs_reload(server_patch):
    """If the re-read session also changes before its facilitation mark (or
    cannot be loaded), the call cannot say who holds the session: not a
    success, and never the exited reviewer's move. A session reassigned to a
    replacement reviewer in between is never marked on the exited reviewer's
    behalf (review of 68d26e28a)."""
    dispatch = AsyncMock(return_value={"ok": True, "execution_id": "ex-claim-then-crash"})
    reassigned = _reviewer_claimed_session()
    reassigned.reviewer_agent_id = "reviewer-replacement"
    advanced = _reviewer_claimed_session()
    advanced.transcript_revision = 3

    for fresh, marks in (
        (_reviewer_claimed_session(), 2),  # the mark itself is refused
        (reassigned, 1),
        (advanced, 1),
        (None, 1),
    ):
        session, data, _append, mark, emit = await _submit_thesis_while_reviewer_claims_slot(
            dispatch=dispatch, crashed_fast=AsyncMock(return_value=True),
            readback=dict(_REVIEWER_CLAIMED_ROW), fresh_session=fresh,
        )
        assert data["success"] is False, data
        assert data["thesis_recorded"] is True
        assert data["reviewer_exited"] is True
        assert data["state_reload_required"] is True
        assert data["awaiting_facilitation"] is False
        assert "Reload" in data["next_step"]
        assert mark.await_count == marks
        assert "dialectic_facilitation_needed" not in [
            c.args[0] for c in emit.await_args_list
        ]


@pytest.mark.asyncio
async def test_dispatch_timeout_after_spawn_leaves_thesis_recorded(server_patch):
    """#2905, timeout trigger: the dispatch call times out on our side after
    the orchestrator spawned the reviewer, so it returns None and the
    fast-crash check never runs. The spawned reviewer's claim lands before the
    in-process antithesis, and the outcome must be the same truthful report."""
    dispatch = AsyncMock(return_value=None)
    crashed_fast = AsyncMock(return_value=False)

    session, data, append, mark, emit = await _submit_thesis_while_reviewer_claims_slot(
        dispatch=dispatch, crashed_fast=crashed_fast, readback=dict(_REVIEWER_CLAIMED_ROW),
    )

    _assert_recorded_thesis_with_reviewer_in_charge(session, data, append, mark, emit, dispatch)
    crashed_fast.assert_not_awaited()
    assert "orchestrated_review" not in data


@pytest.mark.asyncio
async def test_refused_facilitation_without_visible_advance_needs_reload(server_patch):
    """If the facilitation mark is refused but the re-read shows no other
    writer (or cannot be read), the slot may still be open with no request on
    record. That is not a success, but the thesis is still recorded."""
    dispatch = AsyncMock(return_value=None)
    crashed_fast = AsyncMock(return_value=False)
    unchanged = dict(
        _REVIEWER_CLAIMED_ROW, phase="antithesis", reviewer=None, transcript_revision=1,
    )

    for readback in (unchanged, None):
        session, data, _append, mark, emit = await _submit_thesis_while_reviewer_claims_slot(
            dispatch=dispatch, crashed_fast=crashed_fast, readback=readback,
        )
        assert data["success"] is False
        assert data["thesis_recorded"] is True
        assert data["state_reload_required"] is True
        assert "session_advanced" not in data
        assert "error" not in data
        assert "Reload" in data["next_step"]
        mark.assert_awaited_once()
        assert "dialectic_facilitation_needed" not in [
            c.args[0] for c in emit.await_args_list
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["crash", "timeout"])
async def test_reviewer_terminal_failure_during_fallback_is_not_success(server_patch, trigger):
    """#2905 review (P1): the spawned reviewer can commit a terminal failure
    (here a hard-limit block) before crashing or before the dispatch timeout
    is observed. The recovery report keeps thesis_recorded but must carry the
    failure exactly as the synthetic timeout path does, not success."""
    dispatch = AsyncMock(
        return_value={"ok": True, "execution_id": "ex-terminal"} if trigger == "crash" else None
    )
    failed_row = dict(
        _REVIEWER_CLAIMED_ROW,
        phase="failed",
        status="failed",
        resolution={"action": "block", "reason": "hard limit: unsafe resume"},
        resolution_effect_state=None,
    )

    _session, data, _append, mark, emit = await _submit_thesis_while_reviewer_claims_slot(
        dispatch=dispatch, crashed_fast=AsyncMock(return_value=True), readback=failed_row,
    )

    assert data["success"] is False
    assert data["thesis_recorded"] is True
    assert data["session_advanced"] is True
    assert data["phase"] == "failed"
    assert data["terminal_status"] == "failed"
    assert data["terminal_committed"] is True
    assert data["action"] == "block"
    assert data["reason"] == "hard limit: unsafe resume"
    assert data["hard_limit_blocked"] is True
    assert data["hard_limit_violation"] == "hard limit: unsafe resume"
    assert "terminal failure committed" in data["next_step"]
    mark.assert_awaited_once()
    assert "dialectic_facilitation_needed" not in [
        c.args[0] for c in emit.await_args_list
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["crash", "timeout"])
async def test_reviewer_resolution_with_unconfirmed_effect_is_not_success(server_patch, trigger):
    """#2905 review (P1): a resolved row whose durable effect is
    ``unknown_partial`` is not a successful operation yet. Report
    effect_pending with reconciliation guidance, as the synthetic timeout
    path does, while keeping thesis_recorded."""
    dispatch = AsyncMock(
        return_value={"ok": True, "execution_id": "ex-partial"} if trigger == "crash" else None
    )
    partial_row = dict(
        _REVIEWER_CLAIMED_ROW,
        phase="resolved",
        status="resolved",
        resolution={"action": "resume", "reason": "conditions agreed"},
        resolution_effect_state="unknown_partial",
    )

    _session, data, _append, _mark, _emit = await _submit_thesis_while_reviewer_claims_slot(
        dispatch=dispatch, crashed_fast=AsyncMock(return_value=True), readback=partial_row,
    )

    assert data["success"] is False
    assert data["thesis_recorded"] is True
    assert data["session_advanced"] is True
    assert data["phase"] == "resolved"
    assert data["terminal_committed"] is True
    assert data["effect_pending"] is True
    assert data["resolution_effect_state"] == "unknown_partial"
    assert "reconcile_effect" in data["next_step"]


@pytest.mark.asyncio
async def test_reviewer_resolution_with_applied_effect_stays_success(server_patch):
    """The control for the two cases above: a resolved row whose effect is
    applied is a clean success with no effect pending."""
    applied_row = dict(
        _REVIEWER_CLAIMED_ROW,
        phase="resolved",
        status="resolved",
        resolution={"action": "resume", "reason": "conditions agreed"},
        resolution_effect_state="applied",
    )

    _session, data, _append, _mark, _emit = await _submit_thesis_while_reviewer_claims_slot(
        dispatch=AsyncMock(return_value=None),
        crashed_fast=AsyncMock(return_value=False),
        readback=applied_row,
    )

    assert data["success"] is True
    assert data["thesis_recorded"] is True
    assert data["effect_pending"] is False
    assert data["phase"] == "resolved"
    assert data["resolution"]["action"] == "resume"


@pytest.mark.asyncio
async def test_live_reviewer_is_not_preempted(server_patch):
    """A session with an assigned reviewer keeps the multi-agent path: no
    synthetic antithesis is generated, phase stays at ANTITHESIS."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id="agent-reviewer")
    ACTIVE_SESSIONS[session.session_id] = session

    gen_anti = AsyncMock(return_value=_antithesis())
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=gen_anti))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis())))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc",
            "proposed_conditions": ["c1"],
        })

    data = parse_result(result)
    assert data["success"] is True
    assert data.get("synthetic_review") is None
    gen_anti.assert_not_awaited()
    assert session.phase == DialecticPhase.ANTITHESIS
    assert session.awaiting_facilitation is False
    assert session.reviewer_agent_id == "agent-reviewer"


@pytest.mark.asyncio
async def test_disabled_via_env_leaves_slot_open(server_patch):
    """UNITARES_DIALECTIC_SYNTHETIC_REVIEWER=0 restores await-facilitation."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    gen_anti = AsyncMock(return_value=_antithesis())
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch.dict("os.environ", {"UNITARES_DIALECTIC_SYNTHETIC_REVIEWER": "0"}))
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=gen_anti))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc",
            "proposed_conditions": ["c1"],
        })

    data = parse_result(result)
    assert data["success"] is True
    assert data.get("synthetic_review") is None
    gen_anti.assert_not_awaited()
    assert session.phase == DialecticPhase.ANTITHESIS
    assert data["awaiting_facilitation"] is True
    assert session.awaiting_facilitation is True


@pytest.mark.asyncio
async def test_llm_unavailable_degrades_gracefully(server_patch):
    """If the local model is down, the thesis still records and the session is
    left for a peer/operator — no error surfaced to the caller."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches()[:-1]:  # drop the is_llm_available=True patch
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=False)))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc",
            "proposed_conditions": ["c1"],
        })

    data = parse_result(result)
    assert data["success"] is True
    assert data.get("synthetic_review") is None
    assert session.phase == DialecticPhase.ANTITHESIS
    assert data["awaiting_facilitation"] is True
    assert session.awaiting_facilitation is True


@pytest.mark.asyncio
async def test_unresolved_reviewer_hosts_never_let_the_synthetic_reviewer_approve(
    server_patch, monkeypatch, tmp_path
):
    """inference-hosts-config-v0 step 3: when the server cannot resolve a
    reviewer host list (here its inference hosts file does not load), the
    in-process fallback must not run either, or a dispatch failure would hand
    approval to the local reviewer. The session awaits facilitation instead."""
    from src import inference_config
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    bad = tmp_path / "inference.yaml"
    bad.write_text("version: 1\njobs:\n  summarize: { prefer: [ollama:local] }\n")
    bad.chmod(0o600)
    monkeypatch.setenv(inference_config.HOSTS_FILE_ENV, str(bad))
    inference_config._cache.update(key=None, result=None)

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        approve_anti = AsyncMock(return_value=_antithesis("refine"))
        approve_syn = AsyncMock(return_value=_synthesis("RESUME"))
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=approve_anti))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=approve_syn))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit without re-reading the file",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })
    inference_config._cache.update(key=None, result=None)

    data = parse_result(result)
    assert data.get("resolved") is not True
    assert "synthetic_review" not in data
    assert data["awaiting_facilitation"] is True
    assert "does not load" in data["reviewer_hosts_error"]
    approve_anti.assert_not_called()
    approve_syn.assert_not_called()
    assert session.phase != DialecticPhase.RESOLVED


@pytest.mark.asyncio
@pytest.mark.parametrize("env_name,value", [
    ("UNITARES_DIALECTIC_REVIEWER_HOST", "codex"),
    ("UNITARES_DIALECTIC_REVIEWER_HOSTS", "codex,claude"),
])
async def test_a_listed_reviewer_host_keeps_the_synthetic_reviewer_from_approving(
    server_patch, monkeypatch, env_name, value
):
    """With a reviewer host list, only a listed host's verdict may approve (the
    reviewer's own rule, host_list.HostPlan.listed). When dispatch to that
    reviewer fails, the in-process fallback is the local floor, so it must not
    resolve the session either: the session awaits facilitation."""
    from src import inference_config
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    monkeypatch.delenv(inference_config.HOSTS_FILE_ENV, raising=False)
    monkeypatch.delenv("UNITARES_DIALECTIC_REVIEWER_HOSTS", raising=False)
    monkeypatch.delenv("UNITARES_DIALECTIC_REVIEWER_HOST", raising=False)
    monkeypatch.setenv(env_name, value)
    inference_config._cache.update(key=None, result=None)

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        approve_anti = AsyncMock(return_value=_antithesis("refine"))
        approve_syn = AsyncMock(return_value=_synthesis("RESUME"))
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=approve_anti))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=approve_syn))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit without re-reading the file",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
        })
    inference_config._cache.update(key=None, result=None)

    data = parse_result(result)
    assert data.get("resolved") is not True
    assert "synthetic_review" not in data
    assert data["awaiting_facilitation"] is True
    assert data["reviewer_hosts"] == value
    approve_anti.assert_not_called()
    approve_syn.assert_not_called()
    assert session.phase != DialecticPhase.RESOLVED


@pytest.mark.parametrize("value", ["", "local", "Ollama", "ollama:local"])
def test_no_list_or_the_floor_by_name_leaves_the_synthetic_reviewer_its_authority(
    monkeypatch, value
):
    """No list (or the floor named as the single host) is the old behaviour:
    the local reviewer keeps approval authority, as host_list reads it."""
    from src import inference_config
    from src.mcp_handlers.dialectic.handlers import _reviewer_fallback_block

    monkeypatch.delenv(inference_config.HOSTS_FILE_ENV, raising=False)
    monkeypatch.delenv("UNITARES_DIALECTIC_REVIEWER_HOSTS", raising=False)
    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOST", value)
    inference_config._cache.update(key=None, result=None)
    try:
        assert _reviewer_fallback_block() == ("", "")
    finally:
        inference_config._cache.update(key=None, result=None)


def test_the_server_reads_floor_names_as_the_reviewer_does():
    from agents.dialectic_reviewer.host_list import FLOOR_NAMES
    from src.mcp_handlers.dialectic.handlers import _REVIEWER_FLOOR_NAMES

    assert _REVIEWER_FLOOR_NAMES == FLOOR_NAMES
