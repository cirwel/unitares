"""Wave 3 instrument v2: every cycle row balances, and a hung cycle still reports.

The Wave 3 gate council (2026-09-27) found the v1 instrument could not be read
as written: a hung periodic cycle emitted nothing (B5), write and probe coverage
could not be reconstructed from the cycle row ("write and probe coverage"), and
a refusal did not say who won (B3). These tests drive the real sweeper -- only
the database and audit edges are replaced -- and assert the properties the
gate's reading depends on:

* write_attempt  == write_succeeded + write_refused + write_error
* write_succeeded == overlap_clean + overlap_detected + overlap_probe_failed
* the per-write `dialectic_guarded_write` rows of a cycle sum to its counts
* a timed-out cycle still emits its row, with error="timeout"
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AUTO = "src.mcp_handlers.dialectic.auto_resolve"


def _old(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _row(session_id: str, *, hours: float = 5, phase: str = "thesis",
         reviewer: str | None = None, awaiting: bool = False) -> dict:
    return {
        "session_id": session_id,
        "updated_at": _old(hours),
        "created_at": _old(hours),
        "paused_agent_id": f"paused-{session_id}",
        "reviewer_agent_id": reviewer,
        "phase": phase,
        "status": "active",
        "awaiting_facilitation": awaiting,
    }


def _server(agents: dict | None = None):
    server = MagicMock()
    server.agent_metadata = agents or {}
    server.load_metadata_async = AsyncMock()
    return server


def _assert_balanced(cycle: dict) -> None:
    assert cycle["write_attempt_count"] == (
        cycle["write_succeeded_count"]
        + cycle["write_refused_count"]
        + cycle["write_error_count"]
    ), cycle
    assert cycle["write_succeeded_count"] == (
        cycle["overlap_clean_count"]
        + cycle["overlap_detected_count"]
        + cycle["overlap_probe_failed_count"]
    ), cycle


class _Recorder:
    """Captures the cycle row and the per-write rows the sweeper emits."""

    def __init__(self) -> None:
        self.cycles: list[dict] = []
        self.writes: list[dict] = []

    async def cycle(self, **kwargs):
        self.cycles.append(kwargs)

    async def write(self, **kwargs):
        self.writes.append(kwargs)


async def _run_mixed_cycle(recorder: _Recorder):
    """One cycle exercising every write and probe outcome at once.

    reap-clean      reap lands, probe finds nothing
    reap-detected   reap lands, probe finds a saga created after the check
    reap-blind      reap lands, probe cannot look
    reap-refused    another writer already failed the row
    reap-raises     the write itself raises
    facilitate      ANTITHESIS with a gone reviewer, no replacement: flag lands
    reassign        ANTITHESIS with a gone reviewer, replacement found
    """
    sessions = [
        _row("reap-clean"),
        _row("reap-detected"),
        _row("reap-blind"),
        _row("reap-refused"),
        _row("reap-raises"),
        _row("facilitate", hours=2.5, phase="antithesis", reviewer="gone-1"),
        _row("reassign", hours=2.5, phase="antithesis", reviewer="gone-2"),
    ]

    async def status_write(session_id, status, winner=None):
        if session_id == "reap-refused":
            winner.update(winner_status="failed", winner_reason="liveness_timeout",
                          row_missing=False)
            return False
        if session_id == "reap-raises":
            raise RuntimeError("connection reset")
        return True

    async def probe(session_id, since):
        assert isinstance(since, datetime), "the probe must be time-correlated"
        if session_id == "reap-detected":
            return {"saga_id": "sg-1", "state": "pg_committed", "created_at": since}
        if session_id == "reap-blind":
            return None
        return {}

    async def select(**kwargs):
        excluded = kwargs.get("exclude_agent_ids") or []
        return "new-reviewer" if "gone-2" in excluded else None

    with patch(f"{AUTO}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO}.probe_saga_since_async", side_effect=probe), \
         patch(f"{AUTO}.update_session_status_async", side_effect=status_write), \
         patch(f"{AUTO}.update_session_reviewer_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO}.mark_awaiting_facilitation_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO}.add_message_async", new_callable=AsyncMock), \
         patch(f"{AUTO}.emit_write_refused", new_callable=AsyncMock), \
         patch(f"{AUTO}.emit_write_overlap", new_callable=AsyncMock), \
         patch(f"{AUTO}.emit_facilitation_needed", new_callable=AsyncMock), \
         patch(f"{AUTO}.emit_reviewer_reassigned", new_callable=AsyncMock), \
         patch(f"{AUTO}.emit_guarded_write", side_effect=recorder.write), \
         patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle), \
         patch(f"{AUTO}.mcp_server", _server({
             "paused-facilitate": SimpleNamespace(status="paused"),
             "paused-reassign": SimpleNamespace(status="paused"),
         })), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", side_effect=select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        return await auto_resolve_stuck_sessions(trigger_source="periodic")


class TestEveryRowBalances:
    @pytest.mark.asyncio
    async def test_both_identities_hold_on_a_mixed_cycle(self):
        recorder = _Recorder()
        await _run_mixed_cycle(recorder)

        assert len(recorder.cycles) == 1
        cycle = recorder.cycles[0]
        _assert_balanced(cycle)
        # And each term is the value the scenario forces, so a balance of
        # zeros cannot pass this test.
        assert cycle["write_attempt_count"] == 7
        assert cycle["write_succeeded_count"] == 5
        assert cycle["write_refused_count"] == 1
        assert cycle["write_error_count"] == 1
        assert cycle["overlap_clean_count"] == 3
        assert cycle["overlap_detected_count"] == 1
        assert cycle["overlap_probe_failed_count"] == 1
        assert cycle["resolved_count"] == 3
        assert cycle["facilitation_count"] == 1
        assert cycle["reassigned_count"] == 1
        assert cycle["error"] is None

    @pytest.mark.asyncio
    async def test_per_write_rows_sum_to_the_cycle_row(self):
        """P1: the per-write rows are the cycle's counts, itemised."""
        recorder = _Recorder()
        await _run_mixed_cycle(recorder)
        cycle = recorder.cycles[0]
        writes = recorder.writes

        assert {w["cycle_id"] for w in writes} == {cycle["cycle_id"]}
        assert cycle["cycle_id"]

        def n(**match):
            return sum(all(w.get(k) == v for k, v in match.items()) for w in writes)

        assert len(writes) == cycle["write_attempt_count"]
        assert n(outcome="succeeded") == cycle["write_succeeded_count"]
        assert n(outcome="refused") == cycle["write_refused_count"]
        assert n(outcome="error") == cycle["write_error_count"]
        assert n(probe_outcome="clean") == cycle["overlap_clean_count"]
        assert n(probe_outcome="detected") == cycle["overlap_detected_count"]
        assert n(probe_outcome="probe_failed") == cycle["overlap_probe_failed_count"]

    @pytest.mark.asyncio
    async def test_per_write_rows_carry_what_the_sweeper_read_and_when(self):
        recorder = _Recorder()
        await _run_mixed_cycle(recorder)
        by_session = {w["session_id"]: w for w in recorder.writes}

        refused = by_session["reap-refused"]
        assert refused["winner_status"] == "failed"
        assert refused["winner_reason"] == "liveness_timeout"

        reassign = by_session["reassign"]
        assert reassign["new_reviewer_agent_id"] == "new-reviewer"
        assert reassign["read_state"]["reviewer_agent_id"] == "gone-2"
        assert reassign["read_state"]["phase"] == "antithesis"

        for w in recorder.writes:
            # decision read <= early check <= write return, all present.
            assert w["decision_read_ts"] <= w["early_check_ts"] <= w["commit_ts"]
        assert by_session["reap-raises"]["error"] == "RuntimeError"


class _Hang:
    """A call that never finishes, standing in for a wedged DB call.

    ``fn`` is a real ``async def`` so ``patch(side_effect=...)`` awaits it
    (an object with an async ``__call__`` is not recognised as one).
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()

    @property
    def fn(self):
        async def _hang(*args, **kwargs):
            await self()
        return _hang


class TestHungCycleStillReports:
    @pytest.mark.asyncio
    async def test_timeout_emits_error_timeout_with_committed_counts(self):
        """B5: a hung cycle emits a row, keeps what it committed, and balances.

        The first session is reaped; the second session's write hangs. The
        timeout must cancel the hang and the row must say "timeout", count the
        interrupted write as an error, and keep the earlier reap.
        """
        recorder = _Recorder()
        hang = _Hang()

        async def status_write(session_id, status, winner=None):
            if session_id == "wedged":
                await hang()
            return True

        with patch(f"{AUTO}.get_active_sessions_async", new_callable=AsyncMock,
                   return_value=[_row("reaped"), _row("wedged")]), \
             patch(f"{AUTO}.has_inflight_saga_async",
                   new_callable=AsyncMock, return_value=False), \
             patch(f"{AUTO}.probe_saga_since_async",
                   new_callable=AsyncMock, return_value={}), \
             patch(f"{AUTO}.update_session_status_async", side_effect=status_write), \
             patch(f"{AUTO}.add_message_async", new_callable=AsyncMock), \
             patch(f"{AUTO}.emit_guarded_write", side_effect=recorder.write), \
             patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle):
            from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
            result = await asyncio.wait_for(
                auto_resolve_stuck_sessions(trigger_source="periodic", timeout_s=0.05),
                timeout=5,
            )

        assert hang.entered.is_set(), "the hang must actually have been reached"
        assert result["error"] == "timeout"
        cycle = recorder.cycles[0]
        assert cycle["error"] == "timeout"
        assert cycle["trigger_source"] == "periodic"
        assert cycle["resolved_count"] == 1, "the reap before the hang is kept"
        assert cycle["write_attempt_count"] == 2
        assert cycle["write_error_count"] == 1
        _assert_balanced(cycle)
        # The interrupted write's row is written after the cancellation
        # completes, so the per-write rows still sum to the cycle's counts,
        # and it says its outcome is unknown and why.
        assert len(recorder.writes) == cycle["write_attempt_count"]
        wedged = [w for w in recorder.writes if w["session_id"] == "wedged"]
        assert len(wedged) == 1
        assert wedged[0]["outcome"] == "error"
        assert wedged[0]["error"] == "timeout"
        assert wedged[0]["cycle_id"] == cycle["cycle_id"]

    @pytest.mark.asyncio
    async def test_a_hang_in_the_probe_counts_as_a_failed_probe(self):
        recorder = _Recorder()
        hang = _Hang()
        with patch(f"{AUTO}.get_active_sessions_async", new_callable=AsyncMock,
                   return_value=[_row("s1")]), \
             patch(f"{AUTO}.has_inflight_saga_async",
                   new_callable=AsyncMock, return_value=False), \
             patch(f"{AUTO}.probe_saga_since_async", side_effect=hang.fn), \
             patch(f"{AUTO}.update_session_status_async",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{AUTO}.add_message_async", new_callable=AsyncMock), \
             patch(f"{AUTO}.emit_guarded_write", side_effect=recorder.write), \
             patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle):
            from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
            await auto_resolve_stuck_sessions(trigger_source="periodic", timeout_s=0.05)

        cycle = recorder.cycles[0]
        assert cycle["error"] == "timeout"
        assert cycle["write_succeeded_count"] == 1
        assert cycle["overlap_probe_failed_count"] == 1
        _assert_balanced(cycle)
        # The write landed; only its probe was cut short. Its row says so.
        assert len(recorder.writes) == 1
        assert recorder.writes[0]["outcome"] == "succeeded"
        assert recorder.writes[0]["probe_outcome"] == "probe_failed"

    @pytest.mark.asyncio
    async def test_external_cancellation_is_not_reported_as_a_timeout(self):
        """A shutdown is not a hung cycle; the row must say which it was."""
        recorder = _Recorder()
        hang = _Hang()
        with patch(f"{AUTO}.get_active_sessions_async", side_effect=hang.fn), \
             patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle):
            from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
            task = asyncio.create_task(
                auto_resolve_stuck_sessions(trigger_source="periodic", timeout_s=60)
            )
            await asyncio.wait_for(hang.entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert recorder.cycles[0]["error"] == "cancelled"

    @pytest.mark.asyncio
    async def test_no_timeout_means_no_bound(self):
        """Lazy and direct callers pass no timeout and behave as before."""
        recorder = _Recorder()
        with patch(f"{AUTO}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[]), \
             patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle):
            from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
            result = await auto_resolve_stuck_sessions(trigger_source="active_session_check")

        assert "error" not in result
        assert recorder.cycles[0]["error"] is None


class TestPeriodicTimeoutSetting:
    def test_default_is_300_seconds(self, monkeypatch):
        from src import background_tasks as bt

        monkeypatch.delenv(bt.DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, raising=False)
        assert bt._dialectic_sweep_cycle_timeout_s() == 300.0

    def test_env_override(self, monkeypatch):
        from src import background_tasks as bt

        monkeypatch.setenv(bt.DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, "45")
        assert bt._dialectic_sweep_cycle_timeout_s() == 45.0

    @pytest.mark.parametrize("raw", ["0", "-5", "soon", "   "])
    def test_unusable_values_fall_back_to_the_default(self, monkeypatch, raw):
        from src import background_tasks as bt

        monkeypatch.setenv(bt.DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, raw)
        assert bt._dialectic_sweep_cycle_timeout_s() == 300.0

    @pytest.mark.asyncio
    async def test_the_periodic_cycle_passes_the_bound(self, monkeypatch):
        from src import background_tasks as bt

        monkeypatch.setenv(bt.DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, "12")
        resolver = AsyncMock(return_value={})
        with patch(f"{AUTO}.auto_resolve_stuck_sessions", new=resolver):
            await bt._run_dialectic_auto_resolve_cycle()
        resolver.assert_awaited_once_with(trigger_source="periodic", timeout_s=12.0)


@pytest.mark.asyncio
async def test_a_write_interrupted_by_shutdown_still_gets_its_row():
    """External cancellation: the owed per-write row says "cancelled"."""
    recorder = _Recorder()
    hang = _Hang()

    async def status_write(session_id, status, winner=None):
        await hang()

    with patch(f"{AUTO}.get_active_sessions_async", new_callable=AsyncMock,
               return_value=[_row("s1")]), \
         patch(f"{AUTO}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO}.update_session_status_async", side_effect=status_write), \
         patch(f"{AUTO}.emit_guarded_write", side_effect=recorder.write), \
         patch(f"{AUTO}.emit_sweep_cycle", side_effect=recorder.cycle):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        task = asyncio.create_task(
            auto_resolve_stuck_sessions(trigger_source="periodic", timeout_s=60))
        await asyncio.wait_for(hang.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    cycle = recorder.cycles[0]
    assert cycle["error"] == "cancelled"
    _assert_balanced(cycle)
    assert len(recorder.writes) == cycle["write_attempt_count"] == 1
    assert (recorder.writes[0]["outcome"], recorder.writes[0]["error"]) == ("error", "cancelled")
