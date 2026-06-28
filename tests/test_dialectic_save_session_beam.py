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
