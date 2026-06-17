"""Tests for the end-to-end synthetic-reviewer completion in submit_thesis.

When a dialectic session has no independent live reviewer (the default, since
auto-select is disabled), submit_thesis drives the session to a resolved
synthesis via the local synthetic reviewer instead of stranding it at
awaiting_facilitation. These tests pin that completion, the multi-agent
preservation (a real reviewer is never pre-empted), and the env opt-out.
"""

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


def _antithesis():
    return {
        "concerns": ["risk_score alone ignores trajectory", "removes an audit checkpoint"],
        "counter_reasoning": "Low instantaneous risk is not a safe trajectory.",
        "grounding_cited": "coherence 0.38 + entropy 0.6",
        "position": "dispute",
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


def _common_patches():
    """Patch the persistence + auth seams used by submit_thesis."""
    return [
        patch(f"{DIALECTIC}._resolve_dialectic_agent_id",
              new=AsyncMock(return_value=("agent-paused", None))),
        patch(f"{DIALECTIC}.load_session", new=AsyncMock(return_value=None)),
        patch(f"{DIALECTIC}.pg_add_message", new=AsyncMock()),
        patch(f"{DIALECTIC}.pg_update_phase", new=AsyncMock()),
        patch(f"{DIALECTIC}.pg_resolve_session", new=AsyncMock()),
        patch(f"{DIALECTIC}.save_session", new=AsyncMock()),
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)),
    ]


@pytest.mark.asyncio
async def test_thesis_with_open_slot_resolves_via_synthetic_reviewer(server_patch):
    """No live reviewer -> submit_thesis runs antithesis+synthesis to RESOLVED."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_antithesis())))
        stack.enter_context(patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthesis("RESUME"))))
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
    assert data["reviewer_agent_id"] == "llm-synthetic-reviewer"
    assert data["recommendation"] == "RESUME"
    assert data["resolved"] is True
    assert data["antithesis"]["position"] == "dispute"
    assert data["synthesis"]["merged_conditions"]
    assert session.phase == DialecticPhase.RESOLVED
    # The synthetic reviewer claimed the open slot.
    assert session.reviewer_agent_id == "llm-synthetic-reviewer"


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
