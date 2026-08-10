"""
Regression: a converged dialectic session must persist its REAL resolution.

`submit_synthesis` sets `phase = RESOLVED` before `finalize_resolution` builds
the resolution object. `handle_submit_synthesis` then flushed the session with
`save_session` while `session.resolution` was still None, so the terminal row
landed with an empty `{}` — and because the terminal write is guarded
(B-4 / the BEAM saga refuse to overwrite an already-terminal row), the real
resolution computed moments later was dropped as a conflict.

Result: every session resolved between 2026-06-28 and 2026-08-10 stored
`resolution_json = {}`. Nothing errored; the dashboard's resolution disclosure
simply opened onto nothing for six weeks.

The guard is `save_session(..., defer_terminal=True)`: on the converged path the
caller owns the terminal write, because only the caller has the resolution.
"""

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import DialecticPhase, DialecticSession
from src.mcp_handlers.dialectic.session import save_session

DIALECTIC = "src.mcp_handlers.dialectic.handlers"


def _agent_meta(status="active"):
    return SimpleNamespace(
        status=status,
        label="Test",
        api_key="key123",
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
    )


def _mock_server():
    mock = MagicMock()
    mock.agent_metadata = {
        "agent-paused": _agent_meta(status="paused"),
        "agent-reviewer": _agent_meta(status="active"),
    }
    mock.monitors = {}
    mock.load_metadata = MagicMock()
    mock.load_metadata_async = AsyncMock()
    mock.project_root = str(project_root)
    return mock


def _terminal_session():
    session = DialecticSession(
        paused_agent_id="paused-agent",
        reviewer_agent_id="reviewer-agent",
        dispute_type="verification",
    )
    # What submit_synthesis leaves behind on convergence: terminal phase, and
    # no resolution object yet.
    session.phase = DialecticPhase.RESOLVED
    session.resolution = None
    return session


@pytest.mark.asyncio
async def test_defer_terminal_suppresses_the_empty_terminal_write():
    session = _terminal_session()

    with patch(
        "src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve",
        new_callable=AsyncMock,
    ) as beam, patch(
        "src.dialectic_db.resolve_session_async", new_callable=AsyncMock
    ) as pg:
        await save_session(session, defer_terminal=True)

    beam.assert_not_awaited()
    pg.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_defer_the_terminal_write_still_happens():
    """The deferral must be opt-in — every other caller keeps its behaviour."""
    session = _terminal_session()

    with patch(
        "src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as beam, patch(
        "src.dialectic_db.resolve_session_async", new_callable=AsyncMock
    ) as pg:
        await save_session(session)

    beam.assert_awaited_once()
    # BEAM took the write, so the Python fallback must not double-write.
    pg.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_terminal_phase_is_unaffected_by_defer_terminal():
    """defer_terminal only gates the TERMINAL write; phase syncs still flow."""
    session = DialecticSession(
        paused_agent_id="paused-agent",
        reviewer_agent_id="reviewer-agent",
        dispute_type="verification",
    )
    session.phase = DialecticPhase.SYNTHESIS

    with patch(
        "src.mcp_handlers.dialectic.beam_resolve_client.beam_update_phase",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as beam_phase:
        await save_session(session, defer_terminal=True)

    beam_phase.assert_awaited_once()


@pytest.mark.asyncio
async def test_converged_synthesis_defers_the_flush_before_finalize():
    """
    Ordering guard on the real handler: drive `handle_submit_synthesis` to
    convergence and assert the pre-finalize flush deferred. Without the
    deferral this flush is the write that lands the empty `{}` and locks the
    session terminal, so the real resolution is refused as a conflict.
    """
    from src.mcp_handlers.dialectic.handlers import (
        ACTIVE_SESSIONS,
        handle_submit_synthesis,
    )

    calls = []

    async def _recording_save(session, *, defer_terminal=False):
        calls.append({"defer_terminal": defer_terminal, "resolution": session.resolution})

    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id="agent-reviewer",
        dispute_type="verification",
    )
    session.phase = DialecticPhase.SYNTHESIS
    session.synthesis_round = 1
    ACTIVE_SESSIONS[session.session_id] = session

    server = _mock_server()
    try:
        with patch(f"{DIALECTIC}.mcp_server", server), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
             patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
             patch(f"{DIALECTIC}.save_session", new=_recording_save), \
             patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
             patch(f"{DIALECTIC}.pg_update_phase", new_callable=AsyncMock), \
             patch(f"{DIALECTIC}.pg_resolve_session", new_callable=AsyncMock), \
             patch(f"{DIALECTIC}.beam_update_phase", new_callable=AsyncMock, return_value=None), \
             patch(f"{DIALECTIC}.beam_resolve", new_callable=AsyncMock, return_value=None), \
             patch(f"{DIALECTIC}.execute_resolution", new_callable=AsyncMock,
                   return_value={"success": True}), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-paused"):
            await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-paused",
                "proposed_conditions": ["Lower threshold to 0.5"],
                "root_cause": "Complexity spike",
                "reasoning": "Agreeing to the reviewer's conditions",
                "agrees": True,
                "api_key": "key",
            })
    finally:
        ACTIVE_SESSIONS.pop(session.session_id, None)

    assert calls, "handle_submit_synthesis should have flushed the session"
    # Any flush that happens while the session is terminal but the resolution
    # has not been built yet MUST be deferred.
    premature = [c for c in calls if c["resolution"] is None and not c["defer_terminal"]]
    assert not premature, (
        "a terminal flush ran before finalize_resolution without defer_terminal — "
        "this is what wrote the empty {} resolution"
    )
