"""
Regression test for the dialectic session JSON snapshot writer in save_session.

The offline debug snapshot did json.dumps(session_data) where session_data
embeds paused_agent_state verbatim — which can carry datetime objects — so every
step logged "Object of type datetime is not JSON serializable" and skipped the
snapshot. default=str fixes it. (DB writes were never affected.)
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
    """save_session (catch-all flush) routes its terminal resolve through BEAM —
    the path that owns the orchestrated-reviewer-flow resolve."""
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


@pytest.mark.asyncio
async def test_save_session_snapshot_serializes_datetime(monkeypatch, tmp_path):
    """A datetime nested in paused_agent_state must serialize (via default=str),
    not raise, so the snapshot is actually written."""
    from datetime import datetime, timezone
    monkeypatch.setattr(sess_mod, "UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT", True, raising=False)
    monkeypatch.setattr(sess_mod, "SESSION_STORAGE_DIR", tmp_path, raising=False)

    sess = SimpleNamespace(
        session_id="snap-dt-1",
        phase=DialecticPhase.THESIS,  # non-terminal -> phase-update branch
        to_dict=lambda: {
            "session_id": "snap-dt-1",
            "created_at": datetime.now(timezone.utc),
            "paused_agent_state": {"last_update": datetime.now(timezone.utc)},
        },
    )
    # Stub the PG phase sync so the test doesn't touch a real DB.
    with patch("src.dialectic_db.update_session_phase_async", new=AsyncMock()):
        await sess_mod.save_session(sess)  # previously raised inside the snapshot writer

    import json as _json
    snap = tmp_path / "snap-dt-1.json"
    assert snap.exists()
    data = _json.loads(snap.read_text())  # valid JSON
    assert data["session_id"] == "snap-dt-1"
    assert isinstance(data["created_at"], str)  # datetime rendered via default=str
