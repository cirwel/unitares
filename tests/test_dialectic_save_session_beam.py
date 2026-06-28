"""
save_session is the catch-all flush that resolves a dialectic session in the
orchestrated-reviewer flow (forensically confirmed 2026-06-28: it, not the
explicit handle_submit_synthesis sites, wrote the terminal row there). These
tests pin that its resolve routes through BEAM with a fail-safe Python fallback.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import DialecticPhase
from src.mcp_handlers.dialectic import session as sess_mod


def _resolved_session():
    return SimpleNamespace(
        session_id="sess-save-1",
        phase=DialecticPhase.RESOLVED,
        paused_agent_id="paused-1",
        reviewer_agent_id="rev-1",
        resolution=SimpleNamespace(to_dict=lambda: {"action": "resume"}),
    )


@pytest.mark.asyncio
async def test_save_session_routes_resolve_through_beam(monkeypatch):
    monkeypatch.setattr(sess_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", False, raising=False)
    beam = AsyncMock(return_value={"ok": True, "status": "resolved"})
    pg = AsyncMock()
    with patch("src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve", beam), \
         patch("src.dialectic_db.resolve_session_async", pg):
        await sess_mod.save_session(_resolved_session())

    beam.assert_awaited_once()
    assert beam.await_args.kwargs["status"] == "resolved"
    assert beam.await_args.kwargs["reviewer_agent_id"] == "rev-1"
    pg.assert_not_awaited()  # BEAM owned it -> no Python write


@pytest.mark.asyncio
async def test_save_session_falls_back_to_python_when_beam_declines(monkeypatch):
    monkeypatch.setattr(sess_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", False, raising=False)
    beam = AsyncMock(return_value=None)  # flag off / unreachable / etc.
    pg = AsyncMock()
    with patch("src.mcp_handlers.dialectic.beam_resolve_client.beam_resolve", beam), \
         patch("src.dialectic_db.resolve_session_async", pg):
        await sess_mod.save_session(_resolved_session())

    beam.assert_awaited_once()
    pg.assert_awaited_once()  # fail-safe fallback
