"""
Tests for the reassign_reviewer MCP handler.

Tests: happy path (manual + auto), validation errors, and integration with
stuck-reviewer detection in get_dialectic_session.
"""

import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)

DIALECTIC = "src.mcp_handlers.dialectic.handlers"

from tests.helpers import parse_result


def _make_mock_server(agents=None):
    mock = MagicMock()
    mock.agent_metadata = agents or {}
    mock.monitors = {}
    mock.load_metadata = MagicMock()
    mock.load_metadata_async = AsyncMock()
    mock.project_root = str(project_root)
    return mock


def _make_agent_meta(status="active", label="Test", api_key="key123", tags=None):
    return SimpleNamespace(
        status=status,
        label=label,
        api_key=api_key,
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
        tags=tags or [],
    )


def _make_session(paused_id="agent-paused", reviewer_id="agent-reviewer",
                  phase=DialecticPhase.ANTITHESIS, session_type="recovery"):
    session = DialecticSession(
        paused_agent_id=paused_id,
        reviewer_agent_id=reviewer_id,
        session_type=session_type,
    )
    session.phase = phase
    return session


# ============================================================================
# handle_reassign_reviewer tests
# ============================================================================


@pytest.mark.asyncio
async def test_reassign_reviewer_manual():
    """Should reassign to a specified agent."""
    session = _make_session()
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-reviewer": _make_agent_meta(status="active"),
        "agent-new": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock) as mock_update, \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-new",
            "reason": "Previous reviewer ended session",
        }))

    assert result["success"] is True
    assert result["new_reviewer_id"] == "agent-new"
    assert result["old_reviewer_id"] == "agent-reviewer"
    assert session.reviewer_agent_id == "agent-new"
    assert "unresponsive" not in result["recovery"]["what_happened"].lower()
    assert "previous reviewer ended session" in result["recovery"]["what_happened"].lower()
    mock_update.assert_called_once_with(session.session_id, "agent-new")


@pytest.mark.asyncio
async def test_reassign_reviewer_auto_select():
    """Should auto-select a reviewer when new_reviewer_id is omitted."""
    session = _make_session()
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-reviewer": _make_agent_meta(status="active"),
        "agent-candidate": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock, return_value="agent-candidate"), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
        }))

    assert result["success"] is True
    assert result["new_reviewer_id"] == "agent-candidate"


@pytest.mark.asyncio
async def test_reassign_reviewer_no_session():
    """Should error when session not found."""
    server = _make_mock_server()

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {}), \
         patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": "nonexistent",
        }))

    assert "not found" in result.get("error", "").lower() or result.get("success") is False


@pytest.mark.asyncio
async def test_reassign_reviewer_wrong_phase():
    """Should reject reassignment during SYNTHESIS phase."""
    session = _make_session(phase=DialecticPhase.SYNTHESIS)
    server = _make_mock_server()

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-new",
        }))

    assert "phase" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_reassign_reviewer_self_assign():
    """Should reject assigning paused agent as its own reviewer."""
    session = _make_session()
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-paused",
        }))

    assert result.get("success") is not True


@pytest.mark.asyncio
async def test_reassign_reviewer_paused_agent():
    """Should reject assigning a paused agent as reviewer."""
    session = _make_session()
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-also-paused": _make_agent_meta(status="paused"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-also-paused",
        }))

    assert "paused" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_reassign_reviewer_no_candidates():
    """Should return facilitation guidance when no auto candidates."""
    session = _make_session()
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
        }))

    assert result.get("success") is not True
    assert "recovery" in result or "facilitation" in str(result).lower()
    assert "action='reassign'" in " ".join(result.get("recovery", {}).get("what_you_can_do", []))


@pytest.mark.asyncio
async def test_reassign_clears_awaiting_facilitation():
    """Reassignment should clear the awaiting_facilitation flag."""
    session = _make_session()
    session.awaiting_facilitation = True
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-new": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-new",
        }))

    assert result["success"] is True
    assert session.awaiting_facilitation is False


@pytest.mark.asyncio
async def test_reassign_adds_transcript_message():
    """Reassignment should add a system message to the transcript."""
    session = _make_session()
    initial_transcript_len = len(session.transcript)
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-new": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-new",
        })

    assert len(session.transcript) == initial_transcript_len + 1
    msg = session.transcript[-1]
    assert msg.agent_id == "system"
    assert "reassigned" in msg.reasoning.lower()


@pytest.mark.asyncio
async def test_reassign_during_thesis_phase():
    """Should allow reassignment during THESIS phase (before antithesis)."""
    session = _make_session(phase=DialecticPhase.THESIS)
    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        "agent-new": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.handlers import handle_reassign_reviewer
        result = parse_result(await handle_reassign_reviewer({
            "session_id": session.session_id,
            "new_reviewer_id": "agent-new",
        }))

    assert result["success"] is True


# ============================================================================
# awaiting_facilitation field tests
# ============================================================================


def test_awaiting_facilitation_default_false():
    """New sessions should have awaiting_facilitation=False."""
    session = DialecticSession(paused_agent_id="a", reviewer_agent_id="b")
    assert session.awaiting_facilitation is False


def test_awaiting_facilitation_in_to_dict():
    """to_dict should include awaiting_facilitation."""
    session = DialecticSession(paused_agent_id="a", reviewer_agent_id="b")
    d = session.to_dict()
    assert "awaiting_facilitation" in d
    assert d["awaiting_facilitation"] is False

    session.awaiting_facilitation = True
    d = session.to_dict()
    assert d["awaiting_facilitation"] is True


# ============================================================================
# Stuck reviewer → re-assignment in get_dialectic_session
# ============================================================================


@pytest.mark.asyncio
async def test_stuck_reviewer_triggers_reassignment():
    """When reviewer is stuck and replacement is available, should reassign."""
    session = _make_session(phase=DialecticPhase.ANTITHESIS)
    # Add a thesis message so check_reviewer_stuck can measure time
    thesis_msg = DialecticMessage(
        phase="thesis",
        agent_id="agent-paused",
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        reasoning="thesis",
        root_cause="test",
        proposed_conditions=["cond1"],
    )
    session.transcript.append(thesis_msg)

    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        # reviewer gone — not in metadata
        "agent-candidate": _make_agent_meta(status="active"),
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None), \
         patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock, return_value="agent-candidate"), \
         patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value="test-bound-agent"):
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session
        result = parse_result(await handle_get_dialectic_session({
            "session_id": session.session_id,
            "check_timeout": True,
        }))

    assert result["success"] is True
    assert result.get("reviewer_reassigned") is True
    assert session.reviewer_agent_id == "agent-candidate"


@pytest.mark.asyncio
async def test_stuck_reviewer_no_replacement_awaits_facilitation():
    """When reviewer is stuck and no replacement, should set awaiting_facilitation."""
    session = _make_session(phase=DialecticPhase.ANTITHESIS)
    thesis_msg = DialecticMessage(
        phase="thesis",
        agent_id="agent-paused",
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        reasoning="thesis",
        root_cause="test",
        proposed_conditions=["cond1"],
    )
    session.transcript.append(thesis_msg)

    server = _make_mock_server({
        "agent-paused": _make_agent_meta(status="paused"),
        # reviewer gone, no candidates
    })

    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch(f"{DIALECTIC}.ACTIVE_SESSIONS", {session.session_id: session}), \
         patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=None), \
         patch(f"{DIALECTIC}.select_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value="test-bound-agent"):
        from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session
        result = parse_result(await handle_get_dialectic_session({
            "session_id": session.session_id,
            "check_timeout": True,
        }))

    assert result["success"] is False
    assert result.get("awaiting_facilitation") is True
    assert session.awaiting_facilitation is True
    # Should NOT be FAILED
    assert session.phase == DialecticPhase.ANTITHESIS


# ============================================================================
# Response helpers tests
# ============================================================================


def test_get_reviewer_reassigned_recovery():
    from src.mcp_handlers.dialectic.responses import get_reviewer_reassigned_recovery
    r = get_reviewer_reassigned_recovery("old-agent", "new-agent")
    assert "reassigned" in r["action"].lower()
    assert "old-agent" in r["what_happened"]
    assert "new-agent" in r["what_happened"]
    assert "get_dialectic_session" not in r["related_tools"]
    assert "dialectic" in r["related_tools"]


def test_get_awaiting_facilitation_recovery():
    from src.mcp_handlers.dialectic.responses import get_awaiting_facilitation_recovery
    r = get_awaiting_facilitation_recovery("session-123")
    assert "facilitation" in r["action"].lower()
    assert "session-123" in str(r["what_you_can_do"])
    assert "get_dialectic_session" not in r["related_tools"]


# ============================================================================
# dialectic_reviewer_reassigned audit emission (Wave-3 prereq PR #9):
# disconfirmer (F)'s reassignment-rate metric had NO event-stream source
# (transcript-only, zero %reassign% audit rows all-time). The single
# chokepoint _apply_reviewer_reassignment now emits it.
# ============================================================================


@pytest.mark.asyncio
async def test_reassignment_emits_audit_event():
    session = _make_session()
    emit = AsyncMock(return_value=True)
    with patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
         patch("src.audit_db.append_audit_event_async", emit):
        from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment
        result = await _apply_reviewer_reassignment(
            session.session_id, session, "agent-new", reason="stuck reviewer",
        )

    assert result["new_reviewer_id"] == "agent-new"
    emit.assert_awaited_once()
    event = emit.await_args.args[0]
    assert event["event_type"] == "dialectic_reviewer_reassigned"
    # Top-level session_id feeds the indexed audit.events column
    # (review fold: nested-only landed the column NULL).
    assert event["session_id"] == session.session_id
    assert event["details"]["session_id"] == session.session_id
    assert event["details"]["old_reviewer_id"] == "agent-reviewer"
    assert event["details"]["new_reviewer_id"] == "agent-new"
    assert event["details"]["reason"] == "stuck reviewer"


@pytest.mark.asyncio
async def test_reassignment_emit_failure_is_fail_soft():
    """The reassignment has already committed when the emit runs —
    observability failure must not propagate."""
    session = _make_session()
    emit = AsyncMock(side_effect=RuntimeError("audit db down"))
    with patch(f"{DIALECTIC}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
         patch("src.audit_db.append_audit_event_async", emit):
        from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment
        result = await _apply_reviewer_reassignment(
            session.session_id, session, "agent-new", reason="r",
        )

    assert result["new_reviewer_id"] == "agent-new"
    assert session.reviewer_agent_id == "agent-new"
