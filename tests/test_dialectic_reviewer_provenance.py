"""Reviewer/model provenance on dialectic verdict records.

The 2026-08-18 replay put reviewer verdicts 36-50% apart by capability tier,
and 0 of 194 live dialectic verdicts recorded which model produced them or
whether a fallback fired. These tests pin the closure of that gap: every
synthetic-reviewer verdict row now carries a namespaced
``observed_metrics["reviewer_backend"]`` stamp (the same key the orchestrated
reviewer already writes), agents can file an OUTSIDE-the-server consult (e.g.
Codex) as a governed verdict via ``reviewer_provenance``, and the synthesis
row — which previously persisted no observed_metrics at all — now has a
provenance surface.
"""

import contextlib

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.dialectic_protocol import DialecticSession, DialecticPhase
from tests.helpers import parse_result

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
LLM = "src.mcp_handlers.support.llm_delegation"


def _make_session(reviewer_id=None, phase=DialecticPhase.THESIS):
    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id=reviewer_id,
        session_type="recovery",
    )
    session.phase = phase
    return session


def _antithesis(position="refine", structured=True, degraded=False):
    return {
        "concerns": ["risk_score alone ignores trajectory"],
        "counter_reasoning": "Low instantaneous risk is not a safe trajectory.",
        "grounding_cited": "coherence 0.38",
        "position": position,
        "suggested_conditions": [],
        "source": "llm_synthetic_reviewer",
        "_structured": structured,
        "_degraded": degraded,
    }


def _synthesis(rec="RESUME"):
    return {
        "agreed_root_cause": "Missing checkpoint",
        "reasoning": "Integrates the checkpoint concern.",
        "merged_conditions": ["Re-read before each edit"],
        "recommendation": rec,
        "source": "llm_synthesis",
        "_structured": True,
        "_degraded": False,
    }


@pytest.fixture
def server_patch():
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    with patch(f"{DIALECTIC}.mcp_server", server):
        yield server


@pytest.fixture(autouse=True)
def clear_sessions():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield
    ACTIVE_SESSIONS.clear()


def _common_patches(add_message_mock):
    return [
        patch(f"{DIALECTIC}._resolve_dialectic_agent_id",
              new=AsyncMock(return_value=("agent-paused", None))),
        patch(f"{DIALECTIC}.load_session", new=AsyncMock(return_value=None)),
        patch(f"{DIALECTIC}.pg_add_message", new=add_message_mock),
        patch(f"{DIALECTIC}.pg_update_phase", new=AsyncMock()),
        patch(f"{DIALECTIC}.pg_resolve_session", new=AsyncMock()),
        patch(f"{DIALECTIC}.save_session", new=AsyncMock()),
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)),
    ]


def _rows_by_type(add_message_mock):
    return {
        call.kwargs["message_type"]: call.kwargs
        for call in add_message_mock.await_args_list
    }


# ---------------------------------------------------------------------------
# Synthetic reviewer: every verdict row is stamped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthetic_verdict_rows_carry_reviewer_backend(server_patch, monkeypatch):
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    add_message = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _common_patches(add_message):
            stack.enter_context(p)
        stack.enter_context(patch(
            f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis())))
        stack.enter_context(patch(
            f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis())))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
        })

    assert parse_result(result)["success"] is True
    rows = _rows_by_type(add_message)
    for row_type in ("antithesis", "synthesis"):
        stamp = rows[row_type]["observed_metrics"]["reviewer_backend"]
        assert stamp["reviewer_kind"] == "in_process_synthetic"
        assert stamp["backend"] == "local_ollama"
        assert stamp["model_used"] == "qwen3:8b"
        assert stamp["structured"] is True
        assert stamp["degraded"] is False


@pytest.mark.asyncio
async def test_degraded_synthetic_antithesis_is_stamped_degraded(server_patch):
    """A structured-path fallback and a first-choice verdict are materially
    different objects; the ledger must distinguish them."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    add_message = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _common_patches(add_message):
            stack.enter_context(p)
        stack.enter_context(patch(
            f"{LLM}.generate_antithesis",
            new=AsyncMock(return_value=_antithesis(structured=False, degraded=True))))
        stack.enter_context(patch(
            f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis())))
        await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit",
            "proposed_conditions": ["Re-read before each edit"],
        })

    stamp = _rows_by_type(add_message)["antithesis"]["observed_metrics"]["reviewer_backend"]
    assert stamp["degraded"] is True
    assert stamp["structured"] is False


# ---------------------------------------------------------------------------
# External consult: an outside-model verdict files as a governed record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_external_consult_provenance_lands_on_antithesis_row(server_patch):
    from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None, phase=DialecticPhase.ANTITHESIS)
    ACTIVE_SESSIONS[session.session_id] = session

    add_message = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _common_patches(add_message):
            stack.enter_context(p)
        stack.enter_context(patch(
            f"{DIALECTIC}._resolve_dialectic_agent_id",
            new=AsyncMock(return_value=("agent-consulting", None))))
        stack.enter_context(patch(f"{DIALECTIC}.beam_update_reviewer", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{DIALECTIC}.pg_update_reviewer", new=AsyncMock()))
        stack.enter_context(patch(f"{DIALECTIC}.beam_update_phase", new=AsyncMock(return_value=None)))
        result = await handle_submit_antithesis({
            "session_id": session.session_id,
            "agent_id": "agent-consulting",
            "observed_metrics": {"risk_score": 0.4},
            "concerns": ["consulted model disputes the root cause"],
            "reasoning": "Filed on behalf of an external consult.",
            "reviewer_provenance": {
                "reviewer_kind": "external_consult",
                "backend": "codex-cli",
                "model_used": "gpt-5",
                "consult_source": "dev-workflow council",
                "api_key": "should-be-dropped",   # not in the allowlist
            },
        })

    assert parse_result(result)["success"] is True
    persisted = _rows_by_type(add_message)["antithesis"]["observed_metrics"]
    # Caller metrics survive alongside the namespaced stamp.
    assert persisted["risk_score"] == 0.4
    stamp = persisted["reviewer_backend"]
    assert stamp["reviewer_kind"] == "external_consult"
    assert stamp["backend"] == "codex-cli"
    assert stamp["model_used"] == "gpt-5"
    assert stamp["consult_source"] == "dev-workflow council"
    assert stamp["degraded"] is False
    assert "api_key" not in stamp


@pytest.mark.asyncio
async def test_synthesis_row_persists_consult_provenance(server_patch):
    """The synthesis row previously persisted no observed_metrics at all."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id="agent-reviewer", phase=DialecticPhase.SYNTHESIS)
    ACTIVE_SESSIONS[session.session_id] = session

    add_message = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _common_patches(add_message):
            stack.enter_context(p)
        stack.enter_context(patch(
            f"{DIALECTIC}._resolve_dialectic_agent_id",
            new=AsyncMock(return_value=("agent-reviewer", None))))
        stack.enter_context(patch(f"{DIALECTIC}.beam_update_phase", new=AsyncMock(return_value=None)))
        result = await handle_submit_synthesis({
            "session_id": session.session_id,
            "agent_id": "agent-reviewer",
            "proposed_conditions": ["gate on coherence > 0.85"],
            "root_cause": "Missing checkpoint",
            "reasoning": "Consulted verdict merged.",
            "agrees": False,
            "reviewer_provenance": {
                "reviewer_kind": "external_consult",
                "backend": "codex-cli",
                "model_used": "gpt-5",
            },
        })

    assert parse_result(result)["success"] is True
    stamp = _rows_by_type(add_message)["synthesis"]["observed_metrics"]["reviewer_backend"]
    assert stamp["reviewer_kind"] == "external_consult"
    assert stamp["model_used"] == "gpt-5"


# ---------------------------------------------------------------------------
# The stamp helper's hygiene
# ---------------------------------------------------------------------------

def test_stamp_allowlists_bounds_and_normalizes_kind():
    from src.mcp_handlers.dialectic.handlers import _reviewer_provenance_stamp

    stamp = _reviewer_provenance_stamp(
        {
            "backend": "x" * 500,
            "model_used": "m",
            "secret_token": "nope",
            "tokens_used": 42,
        },
        kind="not_a_real_kind",
        degraded=True,
    )
    assert len(stamp["backend"]) == 200          # bounded
    assert "secret_token" not in stamp           # allowlisted
    assert stamp["tokens_used"] == 42            # non-strings pass through
    assert stamp["reviewer_kind"] == "agent_submitted"  # unknown kind normalized
    assert stamp["degraded"] is True


def test_orchestrated_reviewer_shape_passes_through_untouched():
    """The orchestrated reviewer writes observed_metrics["reviewer_backend"]
    directly (agents/dialectic_reviewer/reviewer.py); with no explicit
    reviewer_provenance argument the merge must not rewrite it."""
    from src.mcp_handlers.dialectic.handlers import _merge_caller_reviewer_provenance

    original = {"reviewer_backend": {"backend": "codex", "degraded": False}, "risk_score": 0.2}
    merged = _merge_caller_reviewer_provenance(original, None)
    assert merged == original
