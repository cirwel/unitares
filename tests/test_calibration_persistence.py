"""
Tests for calibration save/load without db.close().

Feb 2026 fix: calibration._save() and _load() were calling db.close() on the
shared singleton pool, destroying connections for all concurrent users.
The fix removed db.close() and db.init() from both functions.

These tests verify:
1. save_state() does NOT call db.close()
2. load_state() does NOT call db.close()
3. save_state() does NOT call db.init()
4. load_state() does NOT call db.init()
"""

import asyncio
import json
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def mock_db():
    """Mock the database singleton to track close()/init() calls."""
    db = AsyncMock()
    db.init = AsyncMock()
    db.close = AsyncMock()
    db.update_calibration = AsyncMock(return_value=True)
    db.get_calibration = AsyncMock(return_value={
        "bins": {},
        "complexity_bins": {},
        "tactical_bins": {},
    })
    return db


@pytest.fixture
def calibration_checker(tmp_path, mock_db):
    """Create a CalibrationChecker configured for postgres backend."""
    with patch.dict("os.environ", {
        "UNITARES_CALIBRATION_BACKEND": "postgres",
        "DB_BACKEND": "postgres",
    }):
        with patch("src.db.get_db", return_value=mock_db):
            from src.calibration import CalibrationChecker
            checker = CalibrationChecker(state_file=tmp_path / "cal.json")
            checker._backend = "postgres"
            yield checker, mock_db


class TestCalibrationSaveNoClose:
    """Verify save_state() does not call db.close() or db.init()."""

    def test_save_does_not_close_pool(self, calibration_checker):
        """save_state() must NOT call db.close() — it destroys the shared pool."""
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            checker.save_state()
        mock_db.close.assert_not_called()

    def test_save_does_not_init_pool(self, calibration_checker):
        """save_state() must NOT call db.init() — pool is initialized at startup."""
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            checker.save_state()
        mock_db.init.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_calls_update_calibration(self, calibration_checker):
        """save_state() should still call db.update_calibration().

        _run_async uses asyncio.get_running_loop() + create_task, so we must
        be inside an async context for the DB call to fire.
        """
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            checker.save_state()
            # Allow the created task to execute
            await asyncio.sleep(0)
        mock_db.update_calibration.assert_called_once()


class TestCalibrationLoadNoClose:
    """Verify load_state() does not call db.close() or db.init()."""

    def test_load_does_not_touch_db(self, calibration_checker):
        """load_state() is sync JSON-only and must NOT touch the DB."""
        checker, mock_db = calibration_checker
        checker.load_state()
        mock_db.close.assert_not_called()
        mock_db.init.assert_not_called()
        mock_db.get_calibration.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_async_calls_get_calibration(self, calibration_checker):
        """load_state_async() should call db.get_calibration().

        load_state() is now JSON-only (sync). DB loading happens in
        load_state_async() which runs after the event loop is available.
        """
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            await checker.load_state_async()
        mock_db.get_calibration.assert_called()


class TestSingleFlightWriter:
    """#1375: PG saves must be single-flight with write-time snapshots.

    The old per-call create_task captured state at SCHEDULE time with no
    ordering guarantee — two saves microseconds apart could land out of
    order and persist the older snapshot (observed live 2026-07-26: a
    tactical-bin update vanished from the persisted blob until the next
    save re-persisted it).
    """

    @pytest.mark.asyncio
    async def test_rapid_saves_coalesce_to_one_write_with_latest_state(self, calibration_checker):
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            checker.record_prediction(confidence=0.85, predicted_correct=True, actual_correct=1.0)
            checker.record_tactical_decision(0.85, "proceed", True, signal_source="tests")
            # Both record_* calls invoked save_state before the drain task ran.
            for _ in range(5):
                await asyncio.sleep(0)
        mock_db.update_calibration.assert_called_once()
        persisted = mock_db.update_calibration.call_args.args[0]
        assert persisted["bins"], "strategic bin missing from coalesced write"
        assert persisted["tactical_bins_by_channel"].get("tests"), (
            "tactical channel missing — the exact loss shape #1375 regressions against"
        )
        assert checker._save_dirty is False
        assert checker._pg_writer_running is False

    @pytest.mark.asyncio
    async def test_mutation_during_slow_write_triggers_second_write(self, calibration_checker):
        """State mutated while a PG write is in flight must be re-drained."""
        checker, mock_db = calibration_checker
        gate = asyncio.Event()

        async def slow_update(data):
            await gate.wait()
            return True

        mock_db.update_calibration = AsyncMock(side_effect=slow_update)
        with patch("src.db.get_db", return_value=mock_db):
            checker.record_prediction(confidence=0.85, predicted_correct=True, actual_correct=1.0)
            await asyncio.sleep(0)  # drain starts, blocks in slow_update
            checker.record_tactical_decision(0.85, "proceed", True, signal_source="tests")
            assert checker._save_dirty is True  # marked while write in flight
            gate.set()
            for _ in range(5):
                await asyncio.sleep(0)
        assert mock_db.update_calibration.call_count == 2
        final = mock_db.update_calibration.call_args.args[0]
        assert final["tactical_bins_by_channel"].get("tests"), (
            "second drain iteration must persist the mutation that raced the slow write"
        )
        assert checker._pg_writer_running is False

    def test_no_running_loop_leaves_writer_flag_clear(self, calibration_checker):
        """Sync context (no loop): PG save skips, flag must not wedge True."""
        checker, mock_db = calibration_checker
        with patch("src.db.get_db", return_value=mock_db):
            checker.save_state()
        assert checker._pg_writer_running is False
        assert checker._save_dirty is True  # will drain on next in-loop save
