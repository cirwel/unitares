"""Both reviewer-reassignment producers must land on the (F) event stream.

The reassignment-rate half of §11 criterion 10 is computed from
``dialectic_reviewer_reassigned`` in ``audit.events``. Until 2026-08-22 only
the request-driven path emitted it: ``auto_resolve_stuck_sessions`` wrote
reviewer changes directly and called neither ``_apply_reviewer_reassignment``
nor ``check_reviewer_stuck`` nor ``check_timeout``, so every auto-path
reassignment was absent from the stream — while a comment in ``handlers.py``
described that emission as "the single chokepoint for both the explicit
`dialectic(reassign)` tool and the stuck-reviewer auto path."

These tests exist so that claim cannot silently become false again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.dialectic import events


class TestEmitReviewerReassigned:
    @pytest.mark.asyncio
    async def test_payload_shape(self):
        """session_id must be top-level AND nested; source must be carried."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_reviewer_reassigned(
                session_id="sess-1",
                old_reviewer_id="old-agent",
                new_reviewer_id="new-agent",
                reason="reviewer_unresponsive",
                source="sweeper",
            )

        assert captured["event_type"] == "dialectic_reviewer_reassigned"
        assert captured["agent_id"] == "new-agent"
        # Top-level session_id populates the indexed audit.events column;
        # nested-only would land that column NULL.
        assert captured["session_id"] == "sess-1"
        assert captured["details"]["session_id"] == "sess-1"
        assert captured["details"]["old_reviewer_id"] == "old-agent"
        assert captured["details"]["new_reviewer_id"] == "new-agent"
        assert captured["details"]["source"] == "sweeper"

    @pytest.mark.asyncio
    async def test_is_fail_soft(self):
        """The reassignment has already committed; a failed emit must not raise."""
        with patch(
            "src.audit_db.append_audit_event_async",
            side_effect=RuntimeError("audit down"),
        ):
            await events.emit_reviewer_reassigned(
                session_id="sess-2",
                old_reviewer_id=None,
                new_reviewer_id="new-agent",
                reason="r",
                source="request",
            )  # must not raise

    @pytest.mark.asyncio
    async def test_open_reviewer_slot_is_representable(self):
        """old_reviewer_id is Optional — an open slot is not an error."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_reviewer_reassigned(
                session_id="sess-3",
                old_reviewer_id=None,
                new_reviewer_id="new-agent",
                reason="r",
                source="request",
            )

        assert captured["details"]["old_reviewer_id"] is None


class TestBothProducersEmit:
    """Structural: both call sites must route through the shared helper."""

    def test_sweeper_emits(self):
        from pathlib import Path

        src = Path(events.__file__).parent / "auto_resolve.py"
        text = src.read_text(encoding="utf-8")
        assert "emit_reviewer_reassigned(" in text, (
            "auto_resolve_stuck_sessions must emit the reassignment event — "
            "the (F) baseline is computed from this stream"
        )
        assert 'source="sweeper"' in text

    def test_request_path_emits(self):
        from pathlib import Path

        src = Path(events.__file__).parent / "handlers.py"
        text = src.read_text(encoding="utf-8")
        assert "emit_reviewer_reassigned(" in text
        assert 'source="request"' in text

    def test_event_shape_is_not_forked(self):
        """Neither producer may hand-build the payload — one shape, two callers."""
        from pathlib import Path

        pkg = Path(events.__file__).parent
        for name in ("auto_resolve.py", "handlers.py"):
            text = (pkg / name).read_text(encoding="utf-8")
            assert '"event_type": "dialectic_reviewer_reassigned"' not in text, (
                f"{name} builds the reassignment payload inline; it must call "
                "events.emit_reviewer_reassigned so the shape cannot drift"
            )


class TestEmitWriteRefused:
    """The refusal path must reach a durable channel, not just a counter.

    `skipped_count` has counted guarded writes the database refused since #1804
    added the terminal-state predicates, and it reached nothing durable — not
    `audit.events`, not a metric series, and not even the sweep log line, whose
    condition omitted it. A refusal is the direct observation of two writers
    converging on one row, so "the sweeper has never collided" and "we could
    never have seen a collision" were the same sentence until this event existed.
    """

    @pytest.mark.asyncio
    async def test_payload_shape(self):
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_write_refused(
                session_id="sess-1",
                attempted=events.ATTEMPT_REVIEWER_REASSIGNMENT,
                paused_agent_id="a1",
                source="sweeper",
            )

        assert captured["event_type"] == "dialectic_write_refused"
        assert captured["agent_id"] == "a1"
        # Top-level session_id populates the indexed column; nested-only lands
        # it NULL and makes the row unfindable by session.
        assert captured["session_id"] == "sess-1"
        assert captured["details"] == {
            "session_id": "sess-1",
            "attempted": "reviewer_reassignment",
            "paused_agent_id": "a1",
            "source": "sweeper",
            # Absent winner info is carried as None, never dropped, so a
            # reader can tell "not collected" from a key that never existed.
            "winner_status": None,
            "winner_reason": None,
        }

    @pytest.mark.asyncio
    async def test_payload_names_the_winner(self):
        """B3: a refusal records who won, so benign contention (both writers
        wanted `failed`) is separable from divergent contention."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_write_refused(
                session_id="sess-1",
                attempted=events.ATTEMPT_REAP_FAILED,
                winner_status="failed",
                winner_reason="liveness_timeout",
            )

        assert captured["details"]["winner_status"] == "failed"
        assert captured["details"]["winner_reason"] == "liveness_timeout"

    @pytest.mark.asyncio
    async def test_is_fail_soft(self):
        """An audit outage must not turn a skipped session into a failed sweep."""
        with patch("src.audit_db.append_audit_event_async",
                   side_effect=RuntimeError("audit down")):
            await events.emit_write_refused(
                session_id="sess-1",
                attempted=events.ATTEMPT_REAP_FAILED,
            )  # must not raise

    @pytest.mark.asyncio
    async def test_paused_agent_is_optional(self):
        """A row with no paused agent is still a refusal worth recording."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_write_refused(
                session_id="sess-1",
                attempted=events.ATTEMPT_AWAITING_FACILITATION,
            )

        assert captured["agent_id"] is None
        assert captured["details"]["paused_agent_id"] is None
        assert captured["details"]["source"] == "sweeper", "source defaults to the only producer"

    def test_every_refusal_site_emits(self):
        """All three guarded writes must emit — a silent one is an unobservable collision.

        Counts call sites rather than asserting mere presence: the failure this
        guards against is a fourth refusal path being added later that increments
        the counter and emits nothing, which is exactly how the reassignment
        stream came to be incomplete.
        """
        from pathlib import Path

        text = (Path(events.__file__).parent / "auto_resolve.py").read_text(encoding="utf-8")
        assert text.count("await emit_write_refused(") == text.count("skipped_count += 1"), (
            "every skipped_count increment must be accompanied by an "
            "emit_write_refused call; a refused write that emits nothing is "
            "indistinguishable from a collision that never happened"
        )

    def test_shape_is_not_forked(self):
        """The sweeper must not hand-build this payload."""
        from pathlib import Path

        text = (Path(events.__file__).parent / "auto_resolve.py").read_text(encoding="utf-8")
        assert '"event_type": "dialectic_write_refused"' not in text


class TestEmitSweepCycle:
    """Every real cycle supplies a denominator, including the zero case."""

    @pytest.mark.asyncio
    async def test_payload_shape_includes_zeroes_and_source(self):
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_sweep_cycle(
                trigger_source="periodic",
                active_session_count=0,
                active_session_batch_truncated=False,
                stuck_session_count=0,
                invalid_session_count=0,
                saga_inflight_skip_count=0,
                write_attempt_count=0,
                write_succeeded_count=0,
                write_refused_count=0,
                write_error_count=0,
                overlap_clean_count=0,
                overlap_detected_count=0,
                overlap_probe_failed_count=0,
                resolved_count=0,
                reassigned_count=0,
                facilitation_count=0,
                duration_ms=7,
                cycle_id="cycle-1",
            )

        assert captured["event_type"] == "dialectic_sweep_cycle"
        assert captured["agent_id"] is None
        details = dict(captured["details"])
        # Provenance is stamped by the emitter, so no caller can omit it.
        assert details.pop("process_boot_id") == events.PROCESS_BOOT_ID
        assert isinstance(details.pop("cycle_seq"), int)
        assert details.pop("instrument_version") == "wave3-instrument-v2"
        assert details.pop("code_commit") == events.CODE_COMMIT
        assert details.pop("code_commit_source") in ("git", "env", "unavailable")
        assert isinstance(details.pop("emit_failures_since_last_cycle"), int)
        assert details == {
            "trigger_source": "periodic",
            "active_session_count": 0,
            "active_session_batch_truncated": False,
            "stuck_session_count": 0,
            "invalid_session_count": 0,
            "saga_inflight_skip_count": 0,
            "write_attempt_count": 0,
            "write_succeeded_count": 0,
            "write_refused_count": 0,
            "write_error_count": 0,
            "overlap_clean_count": 0,
            "overlap_detected_count": 0,
            "overlap_probe_failed_count": 0,
            "resolved_count": 0,
            "reassigned_count": 0,
            "facilitation_count": 0,
            "duration_ms": 7,
            "error": None,
            "cycle_id": "cycle-1",
        }

    @pytest.mark.asyncio
    async def test_cycle_seq_is_consumed_even_when_the_audit_write_fails(self):
        """B5: a missing seq inside one boot must mean a lost audit write.

        The number is taken before the write is attempted, so a failed write
        leaves a hole the report can see instead of silently renumbering.
        """
        seqs = []

        async def flaky_append(payload):
            seqs.append(payload["details"]["cycle_seq"])
            if len(seqs) == 2:
                raise RuntimeError("audit down")

        kwargs = dict(
            trigger_source="periodic", active_session_count=0,
            active_session_batch_truncated=False, stuck_session_count=0,
            invalid_session_count=0, saga_inflight_skip_count=0,
            write_attempt_count=0, write_succeeded_count=0,
            write_refused_count=0, write_error_count=0, overlap_clean_count=0,
            overlap_detected_count=0, overlap_probe_failed_count=0,
            resolved_count=0, reassigned_count=0, facilitation_count=0,
            duration_ms=1,
        )
        with patch("src.audit_db.append_audit_event_async", side_effect=flaky_append):
            for _ in range(3):
                await events.emit_sweep_cycle(**kwargs)

        assert seqs[1] == seqs[0] + 1 and seqs[2] == seqs[1] + 1

    @pytest.mark.asyncio
    async def test_the_next_cycle_row_reports_emit_failures_then_resets(self):
        """Every instrument emit that fails -- raised or answered False -- is
        counted and reported once, on the next cycle row."""
        from src.dialectic_session_writes import take_emit_failures

        take_emit_failures()  # start from zero
        answers = iter([RuntimeError("audit down"), False, None, None])
        rows = []

        async def flaky_append(payload):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            rows.append(payload)
            return answer

        kwargs = dict(
            trigger_source="periodic", active_session_count=0,
            active_session_batch_truncated=False, stuck_session_count=0,
            invalid_session_count=0, saga_inflight_skip_count=0,
            write_attempt_count=0, write_succeeded_count=0,
            write_refused_count=0, write_error_count=0, overlap_clean_count=0,
            overlap_detected_count=0, overlap_probe_failed_count=0,
            resolved_count=0, reassigned_count=0, facilitation_count=0,
            duration_ms=1,
        )
        with patch("src.audit_db.append_audit_event_async", side_effect=flaky_append):
            await events.emit_write_refused(session_id="s", attempted="reap_failed")  # raises
            await events.emit_write_refused(session_id="s", attempted="reap_failed")  # False
            await events.emit_sweep_cycle(**kwargs)
            await events.emit_sweep_cycle(**kwargs)

        cycles = [r["details"] for r in rows if r["event_type"] == "dialectic_sweep_cycle"]
        assert [c["emit_failures_since_last_cycle"] for c in cycles] == [2, 0]

    @pytest.mark.asyncio
    async def test_a_failed_cycle_row_hands_its_count_to_the_next(self):
        from src.dialectic_session_writes import record_emit_failure, take_emit_failures

        take_emit_failures()
        record_emit_failure()
        answers = iter([RuntimeError("audit down"), None])
        rows = []

        async def flaky_append(payload):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            rows.append(payload)

        kwargs = dict(
            trigger_source="periodic", active_session_count=0,
            active_session_batch_truncated=False, stuck_session_count=0,
            invalid_session_count=0, saga_inflight_skip_count=0,
            write_attempt_count=0, write_succeeded_count=0,
            write_refused_count=0, write_error_count=0, overlap_clean_count=0,
            overlap_detected_count=0, overlap_probe_failed_count=0,
            resolved_count=0, reassigned_count=0, facilitation_count=0,
            duration_ms=1,
        )
        with patch("src.audit_db.append_audit_event_async", side_effect=flaky_append):
            await events.emit_sweep_cycle(**kwargs)  # lost, carrying 1
            await events.emit_sweep_cycle(**kwargs)

        # 1 carried + the lost row itself.
        assert rows[0]["details"]["emit_failures_since_last_cycle"] == 2

    def test_code_commit_resolution_never_raises(self, monkeypatch):
        """No git, no env: the commit is null and says why, and nothing raises."""
        import subprocess

        def boom(*_a, **_k):
            raise FileNotFoundError("git")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.delenv("UNITARES_BUILD_SHA", raising=False)
        assert events._resolve_code_commit() == (None, "unavailable")
        monkeypatch.setenv("UNITARES_BUILD_SHA", "abc123")
        assert events._resolve_code_commit() == ("abc123", "env")

    @pytest.mark.asyncio
    async def test_wrapper_emits_once_for_an_all_zero_cycle(self):
        from src.mcp_handlers.dialectic import auto_resolve

        result = {
            "resolved_count": 0,
            "reassigned_count": 0,
            "facilitation_count": 0,
            "skipped_count": 0,
            "active_session_count": 0,
            "active_session_batch_truncated": False,
            "stuck_session_count": 0,
            "invalid_session_count": 0,
            "saga_inflight_skip_count": 0,
            "write_attempt_count": 0,
        }
        emitted = AsyncMock()
        with patch.object(auto_resolve, "_auto_resolve_stuck_sessions",
                          new=AsyncMock(return_value=result)), \
             patch.object(auto_resolve, "emit_sweep_cycle", emitted):
            returned = await auto_resolve.auto_resolve_stuck_sessions(
                trigger_source="periodic"
            )

        assert returned is result
        emitted.assert_awaited_once()
        assert emitted.await_args.kwargs["trigger_source"] == "periodic"
        assert emitted.await_args.kwargs["write_attempt_count"] == 0
        assert emitted.await_args.kwargs["write_refused_count"] == 0

    @pytest.mark.asyncio
    async def test_emitter_is_fail_soft(self):
        with patch("src.audit_db.append_audit_event_async",
                   side_effect=RuntimeError("audit down")):
            await events.emit_sweep_cycle(
                trigger_source="active_session_check",
                active_session_count=1,
                active_session_batch_truncated=False,
                stuck_session_count=1,
                invalid_session_count=0,
                saga_inflight_skip_count=1,
                write_attempt_count=0,
                write_succeeded_count=0,
                write_refused_count=0,
                write_error_count=0,
                overlap_clean_count=0,
                overlap_detected_count=0,
                overlap_probe_failed_count=0,
                resolved_count=0,
                reassigned_count=0,
                facilitation_count=0,
                duration_ms=3,
            )  # must not raise


class TestEmitWriteOverlap:
    """The sweeper-first ordering: early check clean, write lands, saga appears.

    Neither the early `saga_inflight_skip_count` nor `dialectic_write_refused`
    can see this case, and `emit_sweep_cycle`'s own docstring named it as
    uncovered before this event existed.
    """

    @pytest.mark.asyncio
    async def test_payload_shape(self):
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_write_overlap(
                session_id="sess-1",
                attempted=events.ATTEMPT_REAP_FAILED,
                paused_agent_id="a1",
                source="sweeper",
            )

        assert captured["event_type"] == "dialectic_write_overlap"
        assert captured["agent_id"] == "a1"
        assert captured["session_id"] == "sess-1"
        assert captured["details"] == {
            # v1 fields, unchanged.
            "session_id": "sess-1",
            "attempted": "reap_failed",
            "paused_agent_id": "a1",
            "source": "sweeper",
            "ordering": "sweeper_wrote_first",
            # v2 additions, None when the caller had no saga row to report.
            "detection": "time_correlated",
            "saga_id": None,
            "saga_state": None,
            "saga_created_at": None,
            "saga_pg_committed_at": None,
            "saga_reverted_at": None,
            "early_check_ts": None,
            "commit_ts": None,
            "saga_started": "unknown",
        }

    @pytest.mark.asyncio
    async def test_payload_places_the_saga_relative_to_the_write(self):
        from datetime import datetime, timedelta, timezone

        checked = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        committed = checked + timedelta(milliseconds=30)
        captured = []

        async def fake_append(payload):
            captured.append(payload["details"])

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            for created in (checked + timedelta(milliseconds=5),
                            committed + timedelta(milliseconds=5)):
                await events.emit_write_overlap(
                    session_id="sess-1",
                    attempted=events.ATTEMPT_REAP_FAILED,
                    saga={"saga_id": "sg", "state": "pg_committed",
                          "created_at": created,
                          "pg_committed_at": created + timedelta(milliseconds=6)},
                    early_check_ts=checked,
                    commit_ts=committed,
                )

        assert [d["saga_started"] for d in captured] == [
            "between_early_check_and_commit",
            "after_commit",
        ]
        assert captured[0]["saga_state"] == "pg_committed"
        assert captured[0]["early_check_ts"] == checked.isoformat()

    @pytest.mark.asyncio
    async def test_is_fail_soft(self):
        """The write already committed; telemetry must not unwind it."""
        with patch("src.audit_db.append_audit_event_async",
                   side_effect=RuntimeError("audit down")):
            await events.emit_write_overlap(
                session_id="sess-1",
                attempted=events.ATTEMPT_REVIEWER_REASSIGNMENT,
            )  # must not raise


class TestEmitGuardedWrite:
    """One durable row per guarded sweeper write, with what it read and when."""

    @pytest.mark.asyncio
    async def test_payload_carries_read_state_times_and_provenance(self):
        from datetime import datetime, timedelta, timezone

        read_at = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_guarded_write(
                session_id="sess-1",
                attempted=events.ATTEMPT_REAP_FAILED,
                outcome="refused",
                cycle_id="cycle-1",
                decision_read_ts=read_at,
                early_check_ts=read_at + timedelta(milliseconds=3),
                commit_ts=read_at + timedelta(milliseconds=9),
                read_state={
                    "session_id": "sess-1",
                    "reviewer_agent_id": "rev-1",
                    "phase": "antithesis",
                    "status": "active",
                    "updated_at": read_at - timedelta(hours=5),
                    "awaiting_facilitation": False,
                },
                paused_agent_id="a1",
                winner_status="failed",
                winner_reason="liveness_timeout",
            )

        assert captured["event_type"] == "dialectic_guarded_write"
        assert captured["session_id"] == "sess-1"
        d = captured["details"]
        assert d["mutation"] == "reap"
        assert d["intended_status"] == "failed"
        assert d["outcome"] == "refused"
        assert d["winner_status"] == "failed"
        assert d["winner_reason"] == "liveness_timeout"
        assert d["read_reviewer_agent_id"] == "rev-1"
        assert d["read_phase"] == "antithesis"
        assert d["read_status"] == "active"
        assert d["read_updated_at"] == (read_at - timedelta(hours=5)).isoformat()
        assert d["decision_read_ts"] == read_at.isoformat()
        assert d["commit_ts"] == (read_at + timedelta(milliseconds=9)).isoformat()
        assert d["cycle_id"] == "cycle-1"
        assert d["process_boot_id"] == events.PROCESS_BOOT_ID
        assert d["instrument_version"] == events.INSTRUMENT_VERSION
        assert d["code_commit"] == events.CODE_COMMIT
        assert d["code_commit_source"] == events.CODE_COMMIT_SOURCE

    def test_every_attempt_names_a_mutation_and_an_intent(self):
        for attempted in (events.ATTEMPT_REAP_FAILED,
                          events.ATTEMPT_AWAITING_FACILITATION,
                          events.ATTEMPT_REVIEWER_REASSIGNMENT):
            assert events.MUTATION_NAME[attempted]
            assert events.INTENDED_STATUS[attempted]

    @pytest.mark.asyncio
    async def test_is_fail_soft(self):
        with patch("src.audit_db.append_audit_event_async",
                   side_effect=RuntimeError("audit down")):
            await events.emit_guarded_write(
                session_id="s", attempted=events.ATTEMPT_REAP_FAILED,
                outcome="succeeded", cycle_id=None, decision_read_ts=None,
                early_check_ts=None, commit_ts=None,
            )  # must not raise
