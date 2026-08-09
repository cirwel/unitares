"""A self-review must not write into the fleet-global calibration pool.

`update_calibration_from_dialectic` is the one writer on the cal_I path with no
evidence-weight gate (the `outcome_event` route is guarded at 0.65). Its bins are
fleet-global -- src/calibration.py carries no agent dimension -- and feed cal_I at
50-60% of EVERY agent's I. `reviewer_mode='self'` makes reviewer == paused agent,
so without this guard an agent could resolve a review of itself and move a term in
every other agent's state vector.

The fixtures below supply a matching audit-log entry so the function reaches
`record_prediction`. Without that, every case bails early for an unrelated reason
and the tests pass with or without the guard -- i.e. prove nothing.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.mcp_handlers.dialectic.calibration as cal_mod


def _session(reviewer, paused, dispute_type="verification"):
    return SimpleNamespace(
        session_id="s-1",
        dispute_type=dispute_type,
        reviewer_agent_id=reviewer,
        paused_agent_id=paused,
        discovery_id="disc-1",
        created_at=datetime.now(timezone.utc),
        resolution=SimpleNamespace(action="resume", conditions=[]),
    )


def _audit_with_confidence():
    """One entry matching the session's discovery_id, carrying a confidence --
    enough for the function to reach the calibration write."""
    return MagicMock(query_audit_log=MagicMock(return_value=[
        {"discovery_id": "disc-1", "confidence": 0.9, "complexity_discrepancy": 0.0},
    ]))


@pytest.mark.asyncio
async def test_peer_review_reaches_the_calibration_write():
    """Control. If this ever stops passing, the tests below stop proving
    anything and must be repaired before being trusted."""
    checker = MagicMock()
    with patch.object(cal_mod, "audit_logger", _audit_with_confidence()), \
         patch.object(cal_mod, "calibration_checker", checker):
        ok = await cal_mod.update_calibration_from_dialectic(_session("agent-b", "agent-a"))
    assert ok is True
    checker.record_prediction.assert_called_once()


@pytest.mark.asyncio
async def test_self_review_is_refused_at_the_same_fixture():
    """Same fixture as the control -- the ONLY difference is reviewer == paused."""
    checker = MagicMock()
    with patch.object(cal_mod, "audit_logger", _audit_with_confidence()), \
         patch.object(cal_mod, "calibration_checker", checker):
        ok = await cal_mod.update_calibration_from_dialectic(_session("agent-a", "agent-a"))
    assert ok is False
    checker.record_prediction.assert_not_called()


@pytest.mark.asyncio
async def test_non_verification_still_short_circuits_first():
    """Ordering: the dispute-type check stays ahead of the new guard."""
    checker = MagicMock()
    with patch.object(cal_mod, "audit_logger", _audit_with_confidence()), \
         patch.object(cal_mod, "calibration_checker", checker):
        ok = await cal_mod.update_calibration_from_dialectic(
            _session("agent-a", "agent-a", dispute_type="exploration")
        )
    assert ok is False
    checker.record_prediction.assert_not_called()
