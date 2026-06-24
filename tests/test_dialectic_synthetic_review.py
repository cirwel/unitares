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


OD = "src.mcp_handlers.dialectic.orchestrator_dispatch"


@pytest.mark.asyncio
async def test_orchestrated_dispatch_skips_in_process(server_patch):
    """Escalation tier (design b): when orchestrated review is enabled and the
    dispatch succeeds, the handler returns the dispatch result WITHOUT running the
    in-process synthetic reviewer (generate_antithesis must not be called)."""
    from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

    session = _make_session(reviewer_id=None)
    ACTIVE_SESSIONS[session.session_id] = session
    gen_anti = AsyncMock(return_value=_antithesis())

    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _common_patches():
            stack.enter_context(p)
        stack.enter_context(patch(f"{LLM}.generate_antithesis", new=gen_anti))
        stack.enter_context(patch(f"{OD}.orchestrated_review_enabled", return_value=True))
        stack.enter_context(patch(f"{OD}.dispatch_orchestrated_review",
                                  new=AsyncMock(return_value={"ok": True, "agent_id": "agent-rev-1"})))
        # reviewer is running/healthy (not a fast crash) → async path owns it
        stack.enter_context(patch(f"{OD}.reviewer_crashed_fast", new=AsyncMock(return_value=False)))
        result = await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r",
        })

    data = parse_result(result)
    assert data["orchestrated_review"] is True
    assert data["reviewer_dispatch"]["agent_id"] == "agent-rev-1"
    # The in-process reviewer must NOT have run — the orchestrator owns this review.
    gen_anti.assert_not_called()
    assert "resolved" not in data  # slot left open for the spawned reviewer


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
