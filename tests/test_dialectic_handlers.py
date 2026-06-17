"""
Comprehensive tests for dialectic MCP handlers (src/mcp_handlers/dialectic.py).

Tests the 6 key handler functions:
1. handle_request_dialectic_review - Create dialectic session
2. handle_submit_thesis - Submit thesis in session
3. handle_submit_antithesis - Submit antithesis
4. handle_submit_synthesis - Submit synthesis
5. handle_list_dialectic_sessions - List all sessions
6. handle_get_dialectic_session - Get session by ID or agent

Each handler is tested for: happy path, missing required args, error/exception handling.

All database and external calls are mocked - no PostgreSQL required.
"""

import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

# Ensure project root is on sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
    Resolution,
)
from mcp.types import TextContent


# ============================================================================
# Helpers
# ============================================================================

from tests.helpers import parse_result


def _make_mock_server(agents=None):
    """Create a mock mcp_server with agent_metadata."""
    mock = MagicMock()
    mock.agent_metadata = agents or {}
    mock.monitors = {}
    mock.load_metadata = MagicMock()
    mock.load_metadata_async = AsyncMock()
    mock.project_root = str(project_root)
    return mock


def _make_agent_meta(status="active", label="Test", api_key="key123"):
    """Create a SimpleNamespace mimicking agent metadata."""
    return SimpleNamespace(
        status=status,
        label=label,
        api_key=api_key,
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
    )


def _make_session(paused_id="agent-paused", reviewer_id="agent-reviewer",
                  phase=DialecticPhase.THESIS, session_type="recovery"):
    """Create a DialecticSession for testing."""
    session = DialecticSession(
        paused_agent_id=paused_id,
        reviewer_agent_id=reviewer_id,
        session_type=session_type,
    )
    session.phase = phase
    return session


# Common patch targets (module-level references in dialectic.py)
DIALECTIC = "src.mcp_handlers.dialectic.handlers"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_server():
    """Provide a mock mcp_server patched into dialectic module and shared."""
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-reviewer": _make_agent_meta(status="active"),
        "agent-active": _make_agent_meta(status="active"),
        "agent-waiting": _make_agent_meta(status="waiting_input"),
        "agent-mediator": _make_agent_meta(status="active"),  # Third-party synthesizer
    })
    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
        yield server


@pytest.fixture
def mock_require_registered():
    """Mock require_registered_agent to return a known agent_id."""
    def _factory(agent_id="agent-paused", error=None):
        return patch(
            f"{DIALECTIC}.require_registered_agent",
            return_value=(agent_id, error),
        )
    return _factory


@pytest.fixture
def mock_verify_ownership():
    """Mock verify_agent_ownership to return True.

    This is imported locally inside handle_request_dialectic_review,
    so we patch it at the source location.
    """
    return patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True)


@pytest.fixture
def mock_pg_create():
    """Mock pg_create_session."""
    return patch(f"{DIALECTIC}.pg_create_session", new_callable=AsyncMock)


@pytest.fixture
def mock_pg_add_message():
    """Mock pg_add_message."""
    return patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock)


@pytest.fixture
def mock_pg_update_phase():
    """Mock pg_update_phase."""
    return patch(f"{DIALECTIC}.pg_update_phase", new_callable=AsyncMock)


@pytest.fixture
def mock_pg_update_reviewer():
    """Mock pg_update_reviewer."""
    return patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock)


@pytest.fixture
def mock_pg_resolve_session():
    """Mock pg_resolve_session."""
    return patch(f"{DIALECTIC}.pg_resolve_session", new_callable=AsyncMock)


@pytest.fixture
def mock_is_in_session():
    """Mock is_agent_in_active_session to return False."""
    return patch(
        f"{DIALECTIC}.is_agent_in_active_session",
        new_callable=AsyncMock,
        return_value=False,
    )


@pytest.fixture
def mock_save_session():
    """Mock save_session."""
    return patch(f"{DIALECTIC}.save_session", new_callable=AsyncMock)


@pytest.fixture
def mock_load_session():
    """Mock load_session."""
    return patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock)


@pytest.fixture
def mock_select_reviewer():
    """Mock select_reviewer used by auto reviewer mode."""
    return patch(
        f"{DIALECTIC}.select_reviewer",
        new_callable=AsyncMock,
        return_value=None,
    )


@pytest.fixture
def mock_context_agent_bound():
    """Bound-context variant: handle_get_dialectic_session now suppresses
    check_timeout for UNBOUND callers (PR #611 — the janitorial sweep is
    a write behind a pre_onboard read), so tests exercising the timeout/
    reassignment paths must simulate a bound caller."""
    return patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="test-bound-agent",
    )


@pytest.fixture
def mock_context_agent():
    """Mock get_context_agent_id used by success_response and error_response.

    This is imported locally from src.mcp_handlers.context in many places,
    so we patch it at its canonical location.
    """
    return patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value=None,
    )


@pytest.fixture(autouse=True)
def clear_active_sessions():
    """Clear ACTIVE_SESSIONS between tests to prevent leakage."""
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield
    ACTIVE_SESSIONS.clear()


# ============================================================================
# 1. handle_request_dialectic_review
# ============================================================================

class TestHandleRequestDialecticReview:
    """Tests for handle_request_dialectic_review handler."""

    @pytest.mark.asyncio
    async def test_happy_path_self_review(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_pg_create, mock_is_in_session, mock_context_agent,
    ):
        """Self-review mode creates session with reviewer = paused agent."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_pg_create as pg_create, mock_is_in_session, mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "Test recovery",
                "reviewer_mode": "self",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["paused_agent_id"] == "agent-paused"
        assert data["reviewer_agent_id"] == "agent-paused"
        assert data["phase"] == "thesis"
        assert data["session_type"] == "review"
        assert "session_id" in data
        pg_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_mode_no_reviewer_leaves_slot_open(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_pg_create, mock_is_in_session, mock_context_agent, mock_select_reviewer,
    ):
        """Auto mode with no eligible reviewer leaves the slot OPEN (not self-review).

        Regression guard: previously this self-assigned the paused agent as its own
        reviewer, which occupied the slot and blocked any later summoned/first-
        responder reviewer (the first-responder guard requires reviewer_agent_id
        is None). The slot must stay claimable and the session flag
        awaiting_facilitation.
        """
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review
        from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_pg_create as pg_create, mock_is_in_session, mock_context_agent, \
             mock_select_reviewer:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "High risk score",
                "reviewer_mode": "auto",
            })

        data = parse_result(result)
        assert data["success"] is True
        # Slot left open so a summoned/first-responder reviewer can claim it.
        assert data["reviewer_agent_id"] is None
        assert data["awaiting_reviewer"] is True
        assert "self-review" not in data["note"].lower()
        # Default (synthetic reviewer ON): the note guides the agent to submit a
        # thesis, which auto-completes via the synthetic reviewer. The slot is
        # still left open at request time so a peer can claim it first.
        assert "thesis" in data["note"].lower()
        # Persisted with a NULL reviewer (not the paused agent).
        assert pg_create.await_args.kwargs["reviewer_agent_id"] is None
        # Session flagged as awaiting an independent reviewer.
        session = ACTIVE_SESSIONS[data["session_id"]]
        assert session.awaiting_facilitation is True

    @pytest.mark.asyncio
    async def test_explicit_self_review_still_self_assigns(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_pg_create, mock_is_in_session, mock_context_agent, mock_select_reviewer,
    ):
        """reviewer_mode='self' remains the deliberate solo escape hatch.

        Even with no eligible auto reviewer, an explicit self request must still
        bind the paused agent as reviewer (the auto path no longer does this
        silently, but the explicit opt-in is preserved)."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_pg_create, mock_is_in_session, mock_context_agent, mock_select_reviewer:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "Solo recovery",
                "reviewer_mode": "self",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["reviewer_agent_id"] == "agent-paused"
        assert data["awaiting_reviewer"] is False

    @pytest.mark.asyncio
    async def test_agent_not_registered(self, mock_server, mock_context_agent):
        """Returns error when agent is not registered."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review
        from src.mcp_handlers.utils import error_response

        err = error_response("Agent not registered")
        with patch(f"{DIALECTIC}.require_registered_agent", return_value=(None, err)), \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "unknown-agent",
            })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_agent_not_found_in_metadata(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_context_agent,
    ):
        """Returns error when agent passes registration but not in metadata."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        # Use an agent_id that is not in mock_server.agent_metadata
        with mock_require_registered("agent-nonexistent"), mock_verify_ownership, \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-nonexistent",
                "_agent_uuid": "agent-nonexistent",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_ownership_verification_fails(
        self, mock_server, mock_require_registered, mock_context_agent,
    ):
        """Returns auth error when ownership verification fails."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False), \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "auth" in data.get("error_code", "").lower() or "auth" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_agent_waiting_input_skipped(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_context_agent,
    ):
        """Agent in waiting_input status is skipped (not stuck)."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-waiting"), mock_verify_ownership, \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-waiting",
                "_agent_uuid": "agent-waiting",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data.get("skipped") is True
        assert "waiting_input" in data.get("reason", "")

    @pytest.mark.asyncio
    async def test_duplicate_session_prevented(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_context_agent,
    ):
        """Returns error if agent already has an active session."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             patch(f"{DIALECTIC}.is_agent_in_active_session",
                   new_callable=AsyncMock, return_value=True), \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert data.get("error_code") == "SESSION_EXISTS"

    @pytest.mark.asyncio
    async def test_pg_create_failure(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_is_in_session, mock_context_agent,
    ):
        """Returns error when PostgreSQL session create fails."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_is_in_session, \
             patch(f"{DIALECTIC}.pg_create_session",
                   new_callable=AsyncMock,
                   side_effect=Exception("DB connection lost")), \
             mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reviewer_mode": "self",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "DB_WRITE_FAILED" in data.get("error_code", "")

    @pytest.mark.asyncio
    async def test_llm_reviewer_mode_delegates(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_is_in_session, mock_context_agent,
    ):
        """reviewer_mode='llm' delegates to handle_llm_assisted_dialectic."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        mock_llm_handler = AsyncMock(return_value=[TextContent(
            type="text", text=json.dumps({"success": True, "message": "LLM dialectic done"})
        )])

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_is_in_session, mock_context_agent, \
             patch(f"{DIALECTIC}.handle_llm_assisted_dialectic", mock_llm_handler):
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "reason": "Test",
                "reviewer_mode": "llm",
            })

        mock_llm_handler.assert_awaited_once()
        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_custom_session_type_and_topic(
        self, mock_server, mock_require_registered, mock_verify_ownership,
        mock_pg_create, mock_is_in_session, mock_context_agent,
    ):
        """Custom session_type, topic, and discovery_id are passed through."""
        from src.mcp_handlers.dialectic.handlers import handle_request_dialectic_review

        with mock_require_registered("agent-paused"), mock_verify_ownership, \
             mock_pg_create as pg_create, mock_is_in_session, mock_context_agent:
            result = await handle_request_dialectic_review({
                "agent_id": "agent-paused",
                "_agent_uuid": "agent-paused",
                "session_type": "dispute",
                "topic": "Knowledge graph accuracy",
                "discovery_id": "disc-123",
                "reviewer_mode": "self",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["session_type"] == "dispute"
        # Verify pg_create was called with correct kwargs
        call_kwargs = pg_create.call_args.kwargs
        assert call_kwargs["session_type"] == "dispute"
        assert call_kwargs["topic"] == "Knowledge graph accuracy"
        assert call_kwargs["discovery_id"] == "disc-123"


# ============================================================================
# 2. handle_submit_thesis
# ============================================================================

class TestHandleSubmitThesis:
    """Tests for handle_submit_thesis handler."""

    @pytest.mark.asyncio
    async def test_happy_path(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Successful thesis submission advances phase to antithesis."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "root_cause": "Complexity spike",
                "proposed_conditions": ["Lower threshold"],
                "reasoning": "Task was too complex",
                "api_key": "key123",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert "next_step" in data
        assert session.phase == DialecticPhase.ANTITHESIS

    @pytest.mark.asyncio
    async def test_missing_session_id(self, mock_context_agent):
        """Returns error when session_id is missing."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis

        with mock_context_agent:
            result = await handle_submit_thesis({
                "agent_id": "agent-paused",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_agent_id_no_bound(self, mock_context_agent):
        """Returns error when agent_id is missing and no bound identity."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis

        with mock_context_agent, \
             patch("src.mcp_handlers.identity.shared.get_bound_agent_id", return_value=None):
            result = await handle_submit_thesis({
                "session_id": "some-session",
            })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_session_not_found(self, mock_server, mock_load_session, mock_context_agent):
        """Returns error when session does not exist."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis

        with mock_server, patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None), \
             mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": "nonexistent-session",
                "agent_id": "agent-paused",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_wrong_agent_submits_thesis(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Thesis from non-paused agent fails (DialecticSession rejects it)."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",  # Wrong agent
                "root_cause": "Something",
                "api_key": "key",
            })

        data = parse_result(result)
        # DialecticSession.submit_thesis returns {"success": False, "error": "Only paused agent can submit thesis"}
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_thesis_wrong_phase(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Thesis in wrong phase fails."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "root_cause": "Something",
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "phase" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_thesis_loads_from_disk_on_miss(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Session not in memory is loaded from disk."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "root_cause": "Loaded from disk",
                "proposed_conditions": ["Monitor for 1 hour"],
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert session.session_id in ACTIVE_SESSIONS

    @pytest.mark.asyncio
    async def test_pg_add_message_failure_nonfatal(
        self, mock_server, mock_pg_update_phase, mock_save_session,
        mock_context_agent,
    ):
        """pg_add_message failure is non-fatal (logged as warning)."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock,
                   side_effect=Exception("DB down")), \
             mock_pg_update_phase, mock_save_session, mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "root_cause": "Test root cause",
                "proposed_conditions": ["Monitor closely"],
                "api_key": "key",
            })

        data = parse_result(result)
        # Still succeeds despite pg failure (non-fatal)
        assert data["success"] is True


# ============================================================================
# 3. handle_submit_antithesis
# ============================================================================

class TestHandleSubmitAntithesis:
    """Tests for handle_submit_antithesis handler."""

    @pytest.mark.asyncio
    async def test_happy_path(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent, mock_pg_update_reviewer,
    ):
        """Successful antithesis submission advances phase to synthesis."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent, mock_pg_update_reviewer:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",
                "observed_metrics": {"risk_score": 0.65},
                "concerns": ["Risk too high", "Coherence dropping"],
                "reasoning": "Agent needs cooldown",
                "api_key": "key456",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert "next_step" in data
        assert session.phase == DialecticPhase.SYNTHESIS

    @pytest.mark.asyncio
    async def test_missing_required_args(self, mock_context_agent):
        """Returns error when session_id and agent_id both missing."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis

        with mock_context_agent, \
             patch("src.mcp_handlers.identity.shared.get_bound_agent_id", return_value=None):
            result = await handle_submit_antithesis({})

        data = parse_result(result)
        assert data["success"] is False
        assert "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_session_not_found(self, mock_server, mock_context_agent):
        """Returns error when session does not exist."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis

        with mock_server, patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None), \
             mock_context_agent:
            result = await handle_submit_antithesis({
                "session_id": "nonexistent",
                "agent_id": "agent-reviewer",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_wrong_agent_submits_antithesis(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Antithesis from non-reviewer agent fails."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",  # Wrong: paused agent, not reviewer
                "concerns": ["Something"],
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "assigned reviewer" in data.get("error", "").lower()
        assert "take_over_if_requested" not in data.get("recovery", {}).get("workflow", "")

    @pytest.mark.asyncio
    async def test_antithesis_rejects_agent_id_override_when_session_bound(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session,
    ):
        """Bound session identity cannot spoof reviewer via agent_id override."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-paused"), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",
                "concerns": ["Trying override"],
                "reasoning": "Attempting reviewer spoof from bound paused identity.",
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "override" in data.get("error", "").lower() or "bound identity" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_antithesis_wrong_phase(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Antithesis in wrong phase fails."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)
        ACTIVE_SESSIONS[session.session_id] = session

        with mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",
                "concerns": ["Test"],
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "phase" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_antithesis_loads_from_disk(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent, mock_pg_update_reviewer,
    ):
        """Session loaded from disk when not in memory."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent, mock_pg_update_reviewer:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",
                "concerns": ["Concern"],
                "reasoning": "This needs further review",
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert session.session_id in ACTIVE_SESSIONS

    @pytest.mark.asyncio
    async def test_antithesis_takeover_reassigns_and_submits(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_pg_update_reviewer,
    ):
        """Bound operator can take over reviewer ownership and answer in one call."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis

        session = _make_session(phase=DialecticPhase.ANTITHESIS)

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-active"), \
             mock_pg_add_message as add_message, mock_pg_update_phase, mock_save_session, mock_pg_update_reviewer:
            result = await handle_submit_antithesis({
                "session_id": session.session_id,
                "agent_id": "agent-active",
                "observed_metrics": {"risk_score": 0.65},
                "concerns": ["Need a conserved control signal"],
                "reasoning": "Verdict semantics should stay uniform.",
                "take_over_if_requested": True,
                "takeover_reason": "Operator requested this bound Codex session to answer directly.",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["reviewer_takeover"]["old_reviewer_id"] == "agent-reviewer"
        assert data["reviewer_takeover"]["new_reviewer_id"] == "agent-active"
        assert data.get("reviewer_auto_assigned") is not True
        assert session.reviewer_agent_id == "agent-active"
        assert add_message.await_count == 2


# ============================================================================
# 4. handle_submit_synthesis
# ============================================================================

class TestHandleSubmitSynthesis:
    """Tests for handle_submit_synthesis handler."""

    @pytest.mark.asyncio
    async def test_happy_path_no_convergence(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Synthesis submission without convergence returns next step."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1
        # We need to add the session to the load_session path since
        # handle_submit_synthesis always reloads from disk first
        ACTIVE_SESSIONS[session.session_id] = session

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Lower threshold to 0.5"],
                "root_cause": "Complexity spike",
                "reasoning": "We should be more lenient",
                "agrees": False,
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_missing_required_args(self, mock_context_agent):
        """Returns error when session_id and agent_id both missing."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        with mock_context_agent, \
             patch("src.mcp_handlers.identity.shared.get_bound_agent_id", return_value=None):
            result = await handle_submit_synthesis({})

        data = parse_result(result)
        assert data["success"] is False
        assert "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_session_not_found(self, mock_server, mock_context_agent):
        """Returns error when session not found anywhere."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        with mock_server, patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None), \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": "nonexistent",
                "agent_id": "agent-paused",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_synthesis_wrong_phase(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Synthesis in wrong phase fails."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.THESIS)

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Test"],
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_synthesis_rejects_non_participant(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Non-participants are rejected from submit_synthesis. The
        appointed_mediator_id field was a ghost (never set anywhere); removing
        it closes the privilege-escalation surface where any registered agent
        could mutate a session in_memory and drive synthesis to convergence.
        """
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-not-a-participant",
                "proposed_conditions": ["Test"],
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not a participant" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_max_rounds_exceeded(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Max synthesis rounds exceeded returns conservative default."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 6  # Over max of 5
        session.max_synthesis_rounds = 5

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Test"],
                "api_key": "key",
            })

        data = parse_result(result)
        # Quorum-escalation path retired; max-rounds always applies the
        # conservative default (FAILED phase, autonomous_resolution=True).
        assert data["success"] is False
        assert data.get("autonomous_resolution") is True
        assert data.get("resolution_type") == "conservative_default"

    @pytest.mark.asyncio
    async def test_convergence_with_resolution(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_pg_resolve_session, mock_context_agent,
    ):
        """When both agents agree, synthesis converges and resolution is created."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1

        # Mock the session's submit_synthesis to indicate convergence
        mock_result = {
            "success": True,
            "converged": True,
            "phase": "resolved",
        }

        # Mock finalize_resolution and check_hard_limits
        mock_resolution = MagicMock()
        mock_resolution.to_dict.return_value = {
            "action": "resume",
            "conditions": ["Lower threshold"],
            "signed_by": ["agent-paused", "agent-reviewer"],
        }

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
             patch.object(session, "submit_synthesis", return_value=mock_result), \
             patch.object(session, "finalize_resolution", return_value=mock_resolution), \
             patch.object(session, "check_hard_limits", return_value=(True, None)), \
             patch(f"{DIALECTIC}.execute_resolution", new_callable=AsyncMock,
                   return_value={"resumed": True}), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_pg_resolve_session, mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Lower threshold"],
                "agrees": True,
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data.get("converged") is True
        assert data.get("action") == "resume"

    @pytest.mark.asyncio
    async def test_convergence_safety_violation(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_pg_resolve_session, mock_context_agent,
    ):
        """Safety violation during convergence blocks resolution."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1

        mock_result = {"success": True, "converged": True, "phase": "resolved"}
        mock_resolution = MagicMock()
        mock_resolution.to_dict.return_value = {"action": "block"}

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
             patch.object(session, "submit_synthesis", return_value=mock_result), \
             patch.object(session, "finalize_resolution", return_value=mock_resolution), \
             patch.object(session, "check_hard_limits", return_value=(False, "Bypass safety check")), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_pg_resolve_session, mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Disable monitoring"],
                "agrees": True,
                "api_key": "key",
            })

        data = parse_result(result)
        assert data.get("action") == "block"
        assert "safety" in data.get("reason", "").lower()

    # ------------------------------------------------------------------
    # Participant-set eligibility gate (security)
    #
    # Background: before this gate, ``submit_synthesis`` called
    # ``_resolve_dialectic_agent_id(arguments)`` with the default
    # ``enforce_session_ownership=False``, so any registered agent could
    # drive a synthesis to convergence and trigger resolution execution
    # — including agents with no stake in the session. The sibling
    # handlers (thesis/antithesis) enforce ownership; the asymmetry was
    # intentional for the "third-party synthesizer" comment at the call
    # site, but without a compensating allow-list it was a privilege
    # escalation surface.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_synthesis_rejects_non_participant(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """A registered agent who is neither paused, reviewer, quorum member,
        nor appointed mediator must be rejected — otherwise any agent on the
        server can push a synthesis to convergence and execute resolution."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-active",  # registered but not a participant
                "proposed_conditions": ["Lower threshold"],
                "root_cause": "test",
                "reasoning": "unauthorized",
                "agrees": True,
                "api_key": "key123",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "participant" in data["error"].lower(), (
            f"expected participant-set rejection, got: {data}"
        )

    # ------------------------------------------------------------------
    # Parameter-name aliasing (regression: silent data loss)
    #
    # Background: dialectic tool surface exposes both `proposed_conditions`
    # (thesis/antithesis/synthesis) and `conditions` (vote). A caller passing
    # `conditions=[...]` to synthesis got the field silently dropped; the
    # synthesis message persisted with empty `proposed_conditions`, then
    # finalize tripped check_hard_limits with "Resolution must include at
    # least one condition", leaving the session in a self-contradictory
    # phase=failed / resolution.action=resume terminal state.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_synthesis_accepts_conditions_alias(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """`conditions=[...]` is accepted as an alias for `proposed_conditions=[...]`.

        The DialecticMessage and pg_add_message call must receive the
        alias-resolved conditions, not an empty list.
        """
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1

        captured = {}

        async def capture_add(**kwargs):
            captured.update(kwargs)
            return None

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             patch(f"{DIALECTIC}.pg_add_message", side_effect=capture_add), \
             mock_pg_update_phase, mock_save_session, mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                # Note: alias `conditions=...`, not `proposed_conditions=...`
                "conditions": ["Aliased condition A", "Aliased condition B"],
                "root_cause": "test alias",
                "reasoning": "passing wrong-but-symmetric param name",
                "agrees": False,  # avoid convergence path; just verify message persistence
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True, (
            f"alias `conditions` must be accepted, got: {data}"
        )
        assert captured.get("proposed_conditions") == [
            "Aliased condition A", "Aliased condition B"
        ], (
            f"pg_add_message must receive alias-resolved conditions, got: "
            f"{captured.get('proposed_conditions')}"
        )

    @pytest.mark.asyncio
    async def test_thesis_accepts_conditions_alias(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """Same alias support on thesis (symmetric — same parameter-name confusion class)."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_thesis

        session = _make_session(phase=DialecticPhase.THESIS)

        captured = {}

        async def capture_add(**kwargs):
            captured.update(kwargs)
            return None

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             patch(f"{DIALECTIC}.pg_add_message", side_effect=capture_add), \
             mock_pg_update_phase, mock_save_session, mock_context_agent:
            result = await handle_submit_thesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "conditions": ["Thesis condition via alias"],
                "root_cause": "test alias on thesis",
                "reasoning": "symmetric alias support",
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert captured.get("proposed_conditions") == ["Thesis condition via alias"]

    # ------------------------------------------------------------------
    # Early-fail on agrees=True with empty conditions
    #
    # Without this gate, the empty-conditions case slipped through to
    # check_hard_limits at finalize time and produced phase=failed with
    # resolution.action=resume — internally inconsistent terminal state.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_synthesis_rejects_agrees_with_empty_conditions(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """`agrees=True` + empty conditions + no prior synthesis must fail before any DB write."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1
        # Transcript has no prior synthesis with conditions.
        session.transcript = []

        add_called = []

        async def capture_add(**kwargs):
            add_called.append(kwargs)
            return None

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             patch(f"{DIALECTIC}.pg_add_message", side_effect=capture_add), \
             mock_pg_update_phase, mock_save_session, mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                # Both empty / missing — confused-caller scenario
                "agrees": True,
                "api_key": "key",
            })

        data = parse_result(result)
        assert data["success"] is False, (
            f"agrees=True with empty conditions must be rejected, got: {data}"
        )
        assert data.get("error_code") == "EMPTY_AGREEMENT", (
            f"expected EMPTY_AGREEMENT error_code, got: {data.get('error_code')}"
        )
        assert not add_called, (
            "pg_add_message must NOT be called on early-fail; persisting the "
            "broken message is what produced the original inconsistent state. "
            f"Got calls: {add_called}"
        )

    @pytest.mark.asyncio
    async def test_synthesis_allows_agrees_when_prior_has_conditions(
        self, mock_server, mock_pg_add_message, mock_pg_update_phase,
        mock_save_session, mock_context_agent,
    ):
        """`agrees=True` with empty conditions is allowed if a prior synthesis supplied them.

        This covers the legitimate convergence pattern: agent A files synthesis
        with conditions, agent B files `agrees=True` with no new conditions to
        signal acceptance of A's terms. The merge logic in finalize_resolution
        pulls A's conditions forward — the early-fail must not block this.
        """
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = _make_session(phase=DialecticPhase.SYNTHESIS)
        session.synthesis_round = 1
        # Prior synthesis from the other agent supplies conditions
        prior = DialecticMessage(
            phase="synthesis",
            agent_id="agent-reviewer",
            timestamp=datetime.now(timezone.utc).isoformat(),
            proposed_conditions=["Prior condition"],
            agrees=True,
        )
        session.transcript = [prior]

        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_pg_add_message, mock_pg_update_phase, mock_save_session, \
             mock_context_agent:
            result = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                # No new conditions — accepting prior agent's terms
                "agrees": True,
                "api_key": "key",
            })

        data = parse_result(result)
        # Either succeeds (convergence) or proceeds to next round — both valid.
        # The point is we don't hit the EMPTY_AGREEMENT early-fail.
        assert data.get("error_code") != "EMPTY_AGREEMENT", (
            f"prior synthesis with conditions should bypass early-fail, got: {data}"
        )


class TestHandleListDialecticSessions:
    """Tests for handle_list_dialectic_sessions handler."""

    @pytest.mark.asyncio
    async def test_happy_path_with_results(self, mock_context_agent):
        """Returns sessions when found."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        mock_sessions = [
            {"session_id": "s1", "phase": "resolved", "paused_agent_id": "a1"},
            {"session_id": "s2", "phase": "failed", "paused_agent_id": "a2"},
        ]

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=mock_sessions), \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["session_count"] == 2
        assert len(data["sessions"]) == 2

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_context_agent):
        """Returns empty list with helpful tip when no sessions found."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=[]), \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["sessions"] == []
        assert "tip" in data

    @pytest.mark.asyncio
    async def test_filters_passed_through(self, mock_context_agent):
        """Filters (agent_id, status, limit) are passed to list_all_sessions."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=[]) as mock_list, \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({
                "agent_id": "agent-1",
                "status": "resolved",
                "limit": 10,
                "include_transcript": True,
            })

        mock_list.assert_awaited_once_with(
            agent_id="agent-1",
            status="resolved",
            limit=10,
            include_transcript=True,
        )

    @pytest.mark.asyncio
    async def test_limit_capped_at_200(self, mock_context_agent):
        """Limit is capped at 200 even if larger value provided."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=[]) as mock_list, \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({
                "limit": 999,
            })

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["limit"] == 200

    @pytest.mark.asyncio
    async def test_exception_returns_error(self, mock_context_agent):
        """Exception during listing returns error response."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   side_effect=Exception("DB error")), \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({})

        data = parse_result(result)
        assert data["success"] is False
        assert "error" in data

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self, mock_context_agent):
        """Default limit is 50 when not specified."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=[]) as mock_list, \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({})

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["limit"] == 50

    @pytest.mark.asyncio
    async def test_filters_in_response(self, mock_context_agent):
        """Response includes filters_applied for transparency."""
        from src.mcp_handlers.dialectic.handlers import handle_list_dialectic_sessions

        with patch(f"{DIALECTIC}.list_all_sessions", new_callable=AsyncMock,
                   return_value=[{"session_id": "s1"}]), \
             mock_context_agent:
            result = await handle_list_dialectic_sessions({
                "agent_id": "a1",
                "status": "failed",
            })

        data = parse_result(result)
        assert data["filters_applied"]["agent_id"] == "a1"
        assert data["filters_applied"]["status"] == "failed"


# ============================================================================
# 6. handle_get_dialectic_session
# ============================================================================

class TestHandleGetDialecticSession:
    """Tests for handle_get_dialectic_session handler."""

    @pytest.mark.asyncio
    async def test_happy_path_by_session_id_in_memory(self, mock_context_agent):
        """Returns session data when found in memory by session_id."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.RESOLVED)
        # Set created_at to recent time so check_timeout won't fire
        session.created_at = datetime.now()
        ACTIVE_SESSIONS[session.session_id] = session

        # Mock load_session_as_dict (fast path) and check_reviewer_stuck
        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   return_value={"session_id": session.session_id, "phase": "resolved",
                                 "paused_agent_id": "agent-paused"}), \
             patch(f"{DIALECTIC}.check_reviewer_stuck", new_callable=AsyncMock,
                   return_value=False), \
             mock_context_agent:
            result = await handle_get_dialectic_session({
                "session_id": session.session_id,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["session_id"] == session.session_id

    @pytest.mark.asyncio
    async def test_no_args_provided(self, mock_context_agent):
        """Returns error when neither session_id nor agent_id provided."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        with mock_context_agent:
            result = await handle_get_dialectic_session({})

        data = parse_result(result)
        assert data["success"] is False
        assert "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_session_not_found_by_id(self, mock_context_agent):
        """Returns error when session_id not found."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   return_value=None), \
             patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=None), \
             mock_context_agent:
            result = await handle_get_dialectic_session({
                "session_id": "nonexistent-session",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_fast_path_no_timeout_check(self, mock_context_agent):
        """check_timeout=False uses fast path via load_session_as_dict."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        fast_dict = {
            "session_id": "fast-session",
            "phase": "antithesis",
            "paused_agent_id": "agent-paused",
            "reviewer_agent_id": "agent-reviewer",
        }

        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   return_value=fast_dict), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-reviewer"):
            result = await handle_get_dialectic_session({
                "session_id": "fast-session",
                "check_timeout": False,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["session_id"] == "fast-session"
        assert data["required_role"] == "reviewer"
        assert data["required_agent_id"] == "agent-reviewer"
        assert data["current_agent_can_submit"] is True

    @pytest.mark.asyncio
    async def test_fast_path_actionability_surfaces_takeover_hint(self, mock_server):
        """Session view should explain when a different bound agent can take over antithesis."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        fast_dict = {
            "session_id": "fast-session",
            "phase": "antithesis",
            "paused_agent_id": "agent-paused",
            "reviewer_agent_id": "agent-reviewer",
        }

        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   return_value=fast_dict), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-active"):
            result = await handle_get_dialectic_session({
                "session_id": "fast-session",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["current_agent_can_submit"] is False
        assert "take_over_if_requested" in data["recommended_action"]

    @pytest.mark.asyncio
    async def test_session_timed_out(
        self, mock_pg_add_message, mock_pg_update_phase, mock_context_agent_bound,
    ):
        """Session that has timed out returns failure with recovery guidance."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.THESIS)
        session.created_at = datetime.now()
        ACTIVE_SESSIONS[session.session_id] = session

        # Mock check_timeout to return a timeout reason
        with patch.object(session, "check_timeout",
                          return_value="Session timeout - total time exceeded 6 hours"), \
             mock_pg_add_message, mock_pg_update_phase, mock_context_agent_bound:
            result = await handle_get_dialectic_session({
                "session_id": session.session_id,
                "check_timeout": True,
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "timeout" in data["error"].lower()
        assert "recovery" in data

    @pytest.mark.asyncio
    async def test_reviewer_stuck_detection(
        self, mock_pg_add_message, mock_pg_update_phase, mock_context_agent_bound,
    ):
        """Reviewer stuck causes session to be marked as failed."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        session.created_at = datetime.now()
        ACTIVE_SESSIONS[session.session_id] = session

        # check_timeout returns None (not timed out), but reviewer is stuck
        with patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             patch.object(session, "check_timeout", return_value=None), \
             patch(f"{DIALECTIC}.check_reviewer_stuck", new_callable=AsyncMock,
                   return_value=True), \
             patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock,
                   return_value=None), \
             mock_pg_add_message, mock_pg_update_phase, mock_context_agent_bound:
            result = await handle_get_dialectic_session({
                "session_id": session.session_id,
                "check_timeout": True,
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "reviewer" in data["error"].lower() or "stuck" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_session_loaded_from_disk(self, mock_context_agent):
        """Session loaded from disk is restored to in-memory cache."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session, ACTIVE_SESSIONS

        session = _make_session(phase=DialecticPhase.RESOLVED)
        session.created_at = datetime.now()

        # Fast path returns None → falls through to load_session slow path
        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   return_value=None), \
             patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             mock_context_agent:
            result = await handle_get_dialectic_session({
                "session_id": session.session_id,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert session.session_id in ACTIVE_SESSIONS

    @pytest.mark.asyncio
    async def test_by_agent_id_found(self, mock_server, mock_context_agent):
        """Find sessions by agent_id via PG query."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session, ACTIVE_SESSIONS

        session = _make_session(
            paused_id="agent-active", reviewer_id="agent-reviewer",
            phase=DialecticPhase.RESOLVED,
        )

        # Mock PG returning the session
        with mock_context_agent, \
             patch(f"{DIALECTIC}.pg_get_all_sessions_by_agent", new_callable=AsyncMock,
                   return_value=[session.to_dict()]):
            result = await handle_get_dialectic_session({
                "agent_id": "agent-active",
            })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_by_agent_id_not_registered(self, mock_server, mock_context_agent):
        """Returns error if no sessions found for agent_id."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        # PG returns empty list for unknown agent
        with mock_context_agent, \
             patch(f"{DIALECTIC}.pg_get_all_sessions_by_agent", new_callable=AsyncMock,
                   return_value=[]):
            result = await handle_get_dialectic_session({
                "agent_id": "nonexistent-agent",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "no dialectic sessions" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_by_agent_id_no_sessions(self, mock_server, mock_context_agent):
        """Returns error when agent exists but has no sessions."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        # PG returns empty list
        with mock_context_agent, \
             patch(f"{DIALECTIC}.pg_get_all_sessions_by_agent", new_callable=AsyncMock,
                   return_value=[]):
            result = await handle_get_dialectic_session({
                "agent_id": "agent-active",
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "no dialectic sessions" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_exception_returns_error(self, mock_context_agent):
        """General exceptions are caught and returned as errors."""
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

        with patch(f"{DIALECTIC}.load_session_as_dict", new_callable=AsyncMock,
                   side_effect=RuntimeError("boom")), \
             mock_context_agent:
            result = await handle_get_dialectic_session({
                "session_id": "any",
            })

        data = parse_result(result)
        assert data["success"] is False


# ============================================================================
# 7. check_reviewer_stuck (helper function)
# ============================================================================

class TestCheckReviewerStuck:
    """Tests for check_reviewer_stuck helper."""

    @pytest.mark.asyncio
    async def test_reviewer_not_found_is_stuck(self, mock_server):
        """Reviewer not in metadata is considered stuck (ANTITHESIS phase)."""
        from src.mcp_handlers.dialectic.handlers import check_reviewer_stuck

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        session.reviewer_agent_id = "nonexistent-reviewer"

        result = await check_reviewer_stuck(session)
        assert result is True

    @pytest.mark.asyncio
    async def test_paused_reviewer_is_stuck(self, mock_server):
        """Paused reviewer is considered stuck (ANTITHESIS phase)."""
        from src.mcp_handlers.dialectic.handlers import check_reviewer_stuck

        mock_server.agent_metadata["agent-reviewer"] = _make_agent_meta(status="paused")
        session = _make_session(phase=DialecticPhase.ANTITHESIS)

        result = await check_reviewer_stuck(session)
        assert result is True

    @pytest.mark.asyncio
    async def test_active_recent_reviewer_not_stuck(self, mock_server):
        """Active reviewer with recent thesis is not stuck."""
        from src.mcp_handlers.dialectic.handlers import check_reviewer_stuck
        from src.dialectic_protocol import DialecticMessage

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        # Add a recent thesis to transcript so get_thesis_timestamp() returns now
        session.transcript.append(DialecticMessage(
            phase="thesis", agent_id="agent-paused",
            timestamp=datetime.now().isoformat(),
            reasoning="test thesis"
        ))

        result = await check_reviewer_stuck(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_active_old_session_is_stuck(self, mock_server):
        """Active reviewer but thesis submitted >2h ago is stuck."""
        from src.mcp_handlers.dialectic.handlers import check_reviewer_stuck
        from src.dialectic_protocol import DialecticMessage

        session = _make_session(phase=DialecticPhase.ANTITHESIS)
        # Add an old thesis to transcript (>2h threshold)
        session.transcript.append(DialecticMessage(
            phase="thesis", agent_id="agent-paused",
            timestamp=(datetime.now() - timedelta(hours=3)).isoformat(),
            reasoning="test thesis"
        ))

        result = await check_reviewer_stuck(session)
        assert result is True


# ============================================================================
# 8. _get_dialectic_next_steps (helper function)
# ============================================================================

class TestGetDialecticNextSteps:
    """Tests for _get_dialectic_next_steps helper."""

    def test_resume_steps(self):
        from src.mcp_handlers.dialectic.handlers import _get_dialectic_next_steps
        steps = _get_dialectic_next_steps("RESUME")
        assert len(steps) == 3
        assert any("resume" in s.lower() for s in steps)

    def test_cooldown_steps(self):
        from src.mcp_handlers.dialectic.handlers import _get_dialectic_next_steps
        steps = _get_dialectic_next_steps("COOLDOWN")
        assert len(steps) == 3
        assert any("pause" in s.lower() for s in steps)

    def test_escalate_steps(self):
        from src.mcp_handlers.dialectic.handlers import _get_dialectic_next_steps
        steps = _get_dialectic_next_steps("ESCALATE")
        assert len(steps) == 3
        assert any("human" in s.lower() for s in steps)

    def test_unknown_defaults_to_escalate(self):
        from src.mcp_handlers.dialectic.handlers import _get_dialectic_next_steps
        steps = _get_dialectic_next_steps("SOMETHING_ELSE")
        assert len(steps) == 3
        # Falls through to ESCALATE branch
        assert any("human" in s.lower() for s in steps)


