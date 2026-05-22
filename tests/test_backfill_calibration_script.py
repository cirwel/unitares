from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.sequential_calibration import SequentialCalibrationTracker


@pytest.fixture
def backfill_module():
    project_root = Path(__file__).resolve().parent.parent
    module_path = project_root / "scripts" / "ops" / "backfill_calibration.py"
    spec = importlib.util.spec_from_file_location("backfill_calibration", module_path)
    assert spec and spec.loader, "could not load backfill_calibration module"
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_calibration"] = module
    spec.loader.exec_module(module)
    return module


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.fetch = AsyncMock(return_value=rows)


class _FakeDB:
    def __init__(self, rows, confidence_lookup):
        self._conn = _FakeConn(rows)
        self._confidence_lookup = confidence_lookup
        self.init = AsyncMock()
        self.get_latest_confidence_before = AsyncMock(side_effect=self._lookup)

    def acquire(self):
        return _Acquire(self._conn)

    async def _lookup(self, before_ts=None, agent_id=None):
        return self._confidence_lookup.get((agent_id, before_ts))


@pytest.mark.asyncio
async def test_backfill_rebuild_is_idempotent_and_preserves_existing_samples(
    backfill_module,
    monkeypatch,
    tmp_path,
):
    state_file = tmp_path / "seq_state.json"
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=1)
    t2 = t0 + timedelta(minutes=2)

    rows = [
        {
            "agent_id": "agent-a",
            "outcome_type": "test_passed",
            "is_bad": False,
            "detail": {
                "reported_confidence": 0.8,
                "eprocess_eligible": True,
                "hard_exogenous_signal": "tests",
                "decision_action": "proceed",
                "prediction_id": "pid-1",
            },
            "ts": t0,
        },
        {
            "agent_id": "agent-a",
            "outcome_type": "test_failed",
            "is_bad": True,
            "detail": {
                "reported_confidence": None,
                "eprocess_eligible": False,
            },
            "ts": t1,
        },
        {
            "agent_id": "agent-b",
            "outcome_type": "task_completed",
            "is_bad": False,
            "detail": {
                "reported_confidence": "0.6",
                "eprocess_eligible": "true",
                "hard_exogenous_signal": "lint",
                "decision_action": "proceed",
            },
            "ts": t2,
        },
    ]
    fake_db = _FakeDB(rows, {("agent-a", t1): 0.65})

    monkeypatch.setattr("src.db.get_db", lambda: fake_db)
    monkeypatch.setattr(
        "src.sequential_calibration.SequentialCalibrationTracker",
        lambda *args, **kwargs: SequentialCalibrationTracker(state_file=state_file),
    )

    result1 = await backfill_module.backfill(apply=True, verbose=False)
    tracker1 = SequentialCalibrationTracker(state_file=state_file)
    metrics1 = tracker1.compute_metrics()

    assert result1["rebuilt_samples"] == 3
    assert result1["included_existing_eligible"] == 2
    assert result1["paired_missing_confidence"] == 1
    assert result1["skipped_no_match"] == 0
    assert metrics1["eligible_samples"] == 3
    assert metrics1["signal_sources"] == {"tests": 2, "lint": 1}
    assert metrics1["last_updated"] == t2.isoformat()

    result2 = await backfill_module.backfill(apply=True, verbose=False)
    tracker2 = SequentialCalibrationTracker(state_file=state_file)
    metrics2 = tracker2.compute_metrics()

    assert result2 == result1
    assert metrics2["eligible_samples"] == 3
    assert metrics2["signal_sources"] == {"tests": 2, "lint": 1}


@pytest.mark.asyncio
async def test_backfill_skips_unmatched_missing_confidence(
    backfill_module,
    monkeypatch,
    tmp_path,
):
    state_file = tmp_path / "seq_state.json"
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)
    rows = [
        {
            "agent_id": "agent-a",
            "outcome_type": "test_failed",
            "is_bad": True,
            "detail": {
                "reported_confidence": None,
                "eprocess_eligible": False,
            },
            "ts": t0,
        }
    ]
    fake_db = _FakeDB(rows, {})

    monkeypatch.setattr("src.db.get_db", lambda: fake_db)
    monkeypatch.setattr(
        "src.sequential_calibration.SequentialCalibrationTracker",
        lambda *args, **kwargs: SequentialCalibrationTracker(state_file=state_file),
    )

    result = await backfill_module.backfill(apply=True, verbose=False)
    tracker = SequentialCalibrationTracker(state_file=state_file)
    metrics = tracker.compute_metrics()

    assert result["rebuilt_samples"] == 0
    assert result["paired_missing_confidence"] == 0
    assert result["skipped_no_match"] == 1
    assert result["tracker_state"]["status"] == "no_data"
    assert "log_evidence" not in result["tracker_state"]
    assert "capped_alarm" not in result["tracker_state"]
    assert metrics["status"] == "no_data"
