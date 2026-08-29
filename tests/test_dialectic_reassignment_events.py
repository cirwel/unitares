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
        }

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
                stuck_session_count=0,
                invalid_session_count=0,
                saga_inflight_skip_count=0,
                write_attempt_count=0,
                write_refused_count=0,
                resolved_count=0,
                reassigned_count=0,
                facilitation_count=0,
                duration_ms=7,
            )

        assert captured["event_type"] == "dialectic_sweep_cycle"
        assert captured["agent_id"] is None
        assert captured["details"] == {
            "trigger_source": "periodic",
            "active_session_count": 0,
            "stuck_session_count": 0,
            "invalid_session_count": 0,
            "saga_inflight_skip_count": 0,
            "write_attempt_count": 0,
            "write_refused_count": 0,
            "resolved_count": 0,
            "reassigned_count": 0,
            "facilitation_count": 0,
            "duration_ms": 7,
            "error": None,
        }

    @pytest.mark.asyncio
    async def test_wrapper_emits_once_for_an_all_zero_cycle(self):
        from src.mcp_handlers.dialectic import auto_resolve

        result = {
            "resolved_count": 0,
            "reassigned_count": 0,
            "facilitation_count": 0,
            "skipped_count": 0,
            "active_session_count": 0,
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
                stuck_session_count=1,
                invalid_session_count=0,
                saga_inflight_skip_count=1,
                write_attempt_count=0,
                write_refused_count=0,
                resolved_count=0,
                reassigned_count=0,
                facilitation_count=0,
                duration_ms=3,
            )  # must not raise
