"""Synthetic-fixture outcomes persist but never train calibration.

Wire-in for the calibration harness (PR #770): a row whose detail marks
``synthetic_calibration_fixture=True`` is recorded for the author's own per-agent
analysis, but must NOT feed ``calibration_checker`` — so a fixture accidentally
pointed at live governance cannot poison the global tactical/strategic channels.
The detail flag alone is a forensic breadcrumb; this guard makes it functional.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline


def _mock_db():
    db = MagicMock()
    db.get_latest_eisv_by_agent_id = AsyncMock(return_value=None)
    db.get_latest_confidence_before = AsyncMock(return_value=None)
    db.record_outcome_event = AsyncMock(return_value="outcome-id")
    return db


async def _run(extra_detail):
    db = _mock_db()
    checker = MagicMock()
    args = {
        "agent_id": "agent-1",
        "outcome_type": "test_passed",          # hard-exogenous -> tactical channel
        "outcome_score": 1.0,
        "is_bad": False,
        "confidence": 0.9,                       # _confidence present
        "verification_source": "external_signal",  # -> evidence_weight 1.0, clears the 0.65 gate
        "detail": {"kind": "test", "tool": "python", "exit_code": 0, **extra_detail},
    }
    with patch("src.db.get_db", return_value=db), \
         patch("src.calibration.calibration_checker", checker):
        payload = await _record_outcome_event_inline(args)
    persisted = db.record_outcome_event.await_args.kwargs["detail"]
    return payload, persisted, checker, db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker_detail",
    [
        {"synthetic_calibration_fixture": True},
        {"do_not_use_for_live_validation": True},
        {"synthetic_negative_control": True},
        {"do_not_persist": True},
        {"prediction_binding": "synthetic_negative_control"},
        {"calibration_excluded": True},
    ],
)
async def test_controlled_fixture_persists_but_excluded_from_calibration(marker_detail):
    _, persisted, checker, db = await _run(marker_detail)
    # the row is still persisted (for per-agent analysis) ...
    db.record_outcome_event.assert_awaited_once()
    assert persisted["calibration_excluded"] is True
    assert persisted["eprocess_eligible"] is False
    # ... but calibration is NOT trained on it, even at evidence_weight 1.0
    checker.record_prediction.assert_not_called()
    checker.record_tactical_decision.assert_not_called()


@pytest.mark.asyncio
async def test_non_synthetic_outcome_still_trains_calibration():
    _, persisted, checker, db = await _run({})  # no synthetic marker = the control
    db.record_outcome_event.assert_awaited_once()
    assert persisted["calibration_excluded"] is False
    checker.record_prediction.assert_called_once()
    checker.record_tactical_decision.assert_called_once()
