"""Tests for agent loop detection patterns and safety-net resume.

Covers:
- Pattern 4: lowered proceed threshold (15 → 10)
- Pattern 7: slow proceed loop (8+ proceed in 5 min)
- _safety_net_resume: fallback auto-resume when dialectic fails
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metadata(
    recent_timestamps: list[str] | None = None,
    recent_decisions: list[str] | None = None,
    created_at: str | None = None,
    loop_cooldown_until: str | None = None,
    recovery_attempt_at: str | None = None,
    tags: list[str] | None = None,
    status: str = "active",
):
    """Build a minimal metadata object for detect_loop_pattern."""
    now = datetime.now()
    meta = SimpleNamespace(
        recent_update_timestamps=recent_timestamps or [],
        recent_decisions=recent_decisions or [],
        loop_cooldown_until=loop_cooldown_until,
        recovery_attempt_at=recovery_attempt_at,
        created_at=created_at or (now - timedelta(hours=1)).isoformat(),
        tags=tags or [],
        status=status,
        api_key="test-key",
        paused_at=now.isoformat() if status == "paused" else None,
        loop_detected_at=None,
        loop_incidents=[],
        total_updates=10,
        last_update=now.isoformat(),
    )

    def add_lifecycle_event(event_type, detail):
        if not hasattr(meta, "_lifecycle_events"):
            meta._lifecycle_events = []
        meta._lifecycle_events.append((event_type, detail))

    meta.add_lifecycle_event = add_lifecycle_event
    return meta


def _timestamps_spaced(count: int, spacing_seconds: float, start_offset_seconds: float = 0) -> list[str]:
    """Generate `count` timestamps spaced evenly, ending near now."""
    now = datetime.now()
    start = now - timedelta(seconds=start_offset_seconds + spacing_seconds * (count - 1))
    return [(start + timedelta(seconds=i * spacing_seconds)).isoformat() for i in range(count)]


# ---------------------------------------------------------------------------
# Pattern 4: lowered proceed threshold (10, was 15)
# ---------------------------------------------------------------------------


class TestPattern4LoweredThreshold:
    """Pattern 4 should now trigger at 10 proceed decisions instead of 15."""

    def test_10_rapid_proceed_decisions_triggers(self):
        """10 proceed decisions within 5 minutes should trigger."""
        timestamps = _timestamps_spaced(10, spacing_seconds=20)  # 180s < 300s threshold
        decisions = ["proceed"] * 10

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert is_loop, f"10 rapid proceed decisions should trigger Pattern 4, got: {reason}"
        assert "Decision loop" in reason

    def test_10_slow_proceed_decisions_does_not_trigger(self):
        """10 proceed decisions spread over hours should NOT trigger (cron agents)."""
        timestamps = _timestamps_spaced(10, spacing_seconds=1800)  # 30min apart
        decisions = ["proceed"] * 10

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert not is_loop, f"Slow proceed decisions should NOT trigger: {reason}"

    def test_9_proceed_decisions_does_not_trigger(self):
        """9 proceed decisions should NOT trigger Pattern 4."""
        timestamps = _timestamps_spaced(10, spacing_seconds=20)
        decisions = ["proceed"] * 9 + ["pause"]

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        # Should NOT trigger Pattern 4 (but might trigger other patterns
        # if timestamps are close — we spaced them 60s apart to avoid that)
        if is_loop:
            assert "Decision loop" not in reason, \
                f"9 proceed decisions should not trigger Pattern 4, but got: {reason}"


# ---------------------------------------------------------------------------
# Pattern 7: slow proceed loop
# ---------------------------------------------------------------------------


class TestPattern7SlowProceedLoop:
    """Pattern 7 detects 8+ proceed decisions within 5 minutes."""

    def test_8_proceed_in_5min_triggers(self):
        """8 proceed decisions in a 5-min window should trigger Pattern 7."""
        # 10 timestamps over ~4 minutes (30s apart), all proceed
        timestamps = _timestamps_spaced(10, spacing_seconds=30)
        decisions = ["proceed"] * 10

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert is_loop, f"8+ proceed in 5 min should trigger, got: {reason}"
        # Could be Pattern 4 or 7 — both should fire for 10 proceeds
        assert "loop" in reason.lower() or "proceed" in reason.lower()

    def test_8_proceed_over_10min_does_not_trigger_pattern7(self):
        """8 proceed decisions spread over 10 minutes should NOT trigger Pattern 7."""
        # 10 timestamps over ~10 minutes (75s apart) — outside the 5-min window
        timestamps = _timestamps_spaced(10, spacing_seconds=75)
        decisions = ["proceed"] * 8 + ["pause", "pause"]

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        if is_loop:
            assert "Slow proceed loop" not in reason, \
                f"8 proceeds over 10 min should not trigger Pattern 7, got: {reason}"

    def test_autonomous_agents_skip_pattern7(self):
        """Autonomous/embodied agents skip decision-based patterns including Pattern 7."""
        timestamps = _timestamps_spaced(10, spacing_seconds=30)
        decisions = ["proceed"] * 10

        meta = _make_metadata(
            recent_timestamps=timestamps,
            recent_decisions=decisions,
            tags=["autonomous"],
        )

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        # Autonomous agents skip patterns 4-7 (decision-based)
        if is_loop:
            assert "Decision loop" not in reason
            assert "Slow proceed loop" not in reason

    def test_embodied_only_agents_skip_pattern7(self):
        """Agents tagged only `embodied` (no `autonomous`, no legacy `anima`) still skip pattern 7."""
        timestamps = _timestamps_spaced(10, spacing_seconds=30)
        decisions = ["proceed"] * 10

        meta = _make_metadata(
            recent_timestamps=timestamps,
            recent_decisions=decisions,
            tags=["embodied"],
        )

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        if is_loop:
            assert "Decision loop" not in reason
            assert "Slow proceed loop" not in reason

    def test_stale_timestamps_do_not_trigger_pattern7(self):
        """8 proceeds in a tight window but all >1h old should NOT re-trigger.

        Regression: a dormant agent with an 8-update burst from days ago would
        re-fire Pattern 7 (and Pattern 4) whenever a new update arrived, because
        the last 10 timestamps still fit within 300s — the detector had no
        freshness floor. Real-world repro: agent 2aa0ec9e burst on 2026-04-17,
        sat idle, then a new update on 2026-04-20 re-flagged the stale 8.
        """
        # 8 timestamps over 200s, but the newest is 2 days old
        timestamps = _timestamps_spaced(
            10, spacing_seconds=25, start_offset_seconds=2 * 86400
        )
        decisions = ["proceed"] * 10

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert not is_loop, (
            f"Stale proceed burst (newest 2 days old) should not trigger Pattern 7, "
            f"got: {reason}"
        )

    def test_stale_timestamps_do_not_trigger_pattern4_proceed(self):
        """Same freshness guard should cover Pattern 4's 10-proceed-in-5-min branch."""
        timestamps = _timestamps_spaced(
            10, spacing_seconds=25, start_offset_seconds=2 * 86400
        )
        decisions = ["proceed"] * 10

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert not is_loop, (
            f"Stale proceed burst should not trigger Pattern 4 either, got: {reason}"
        )


# ---------------------------------------------------------------------------
# Pattern 4 pause branch: freshness guard
# ---------------------------------------------------------------------------


class TestPattern4PauseFreshness:
    """Pause branch must not fire on stale histories.

    Regression: once Pattern 4's pause branch fires, the next update is
    rejected before it can be recorded, so the pause-heavy window never rolls
    over. The agent is blocked indefinitely on a static 5-pause history.
    Seen with Steward (9a6681ec): last successful DB update 2026-04-19 06:49,
    followed by 13+ hours of rejected retries.
    """

    def test_5_pauses_recent_triggers(self):
        """Fresh 5-pause burst still fires — preserves existing behavior."""
        timestamps = _timestamps_spaced(10, spacing_seconds=30)
        decisions = ["pause"] * 5 + ["proceed"] * 5

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert is_loop
        assert "pause" in reason.lower()

    def test_5_pauses_stale_does_not_trigger(self):
        """Old pause burst (newest timestamp >1h old) should NOT block the agent."""
        timestamps = _timestamps_spaced(
            10, spacing_seconds=30, start_offset_seconds=6 * 3600
        )
        decisions = ["pause"] * 5 + ["proceed"] * 5

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=12)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert not is_loop, (
            f"Stale pause burst (newest 6h old) should not lock the agent out, "
            f"got: {reason}"
        )

    def test_unparseable_timestamps_fallback_preserves_detection(self):
        """If timestamps can't be parsed, fall back to firing — rather a false
        positive than silently suppressing a real pause loop."""
        timestamps = ["not-a-timestamp"] * 10
        decisions = ["pause"] * 5 + ["proceed"] * 5

        meta = _make_metadata(recent_timestamps=timestamps, recent_decisions=decisions)

        with (
            patch("src.agent_loop_detection.agent_metadata", {"test-agent": meta}),
            patch("src.agent_process_mgmt.SERVER_START_TIME", datetime.now() - timedelta(hours=1)),
        ):
            from src.agent_loop_detection import detect_loop_pattern
            is_loop, reason = detect_loop_pattern("test-agent")

        assert is_loop
        assert "pause" in reason.lower()


# ---------------------------------------------------------------------------
# _safety_net_resume
# ---------------------------------------------------------------------------


class TestSafetyNetResume:
    """_safety_net_resume should auto-resume safe agents when dialectic fails."""

    @pytest.fixture
    def safe_monitor(self):
        state = SimpleNamespace(coherence=0.05, void_active=False)
        monitor = MagicMock()
        monitor.state = state
        monitor.get_metrics.return_value = {"mean_risk": 0.3}
        return monitor

    @pytest.fixture
    def unsafe_monitor(self):
        state = SimpleNamespace(coherence=0.90, void_active=False)
        monitor = MagicMock()
        monitor.state = state
        monitor.get_metrics.return_value = {"mean_risk": 0.7}
        return monitor

    @pytest.mark.asyncio
    async def test_safe_agent_is_resumed(self, safe_monitor):
        """Low-risk/no-void agent resumes regardless of legacy C(V) direction."""
        meta = _make_metadata(status="paused")

        with (
            patch("src.agent_loop_detection.agent_metadata", {"agent-1": meta}),
            patch("src.agent_loop_detection.monitors", {"agent-1": safe_monitor}),
        ):
            from src.agent_loop_detection import _safety_net_resume
            await _safety_net_resume("agent-1", reason="LLM unavailable")

        assert meta.status == "active"
        assert meta.paused_at is None
        assert meta.loop_cooldown_until is None
        assert any("safety_net_resumed" in str(e) for e in meta._lifecycle_events)

    @pytest.mark.asyncio
    async def test_unsafe_agent_stays_paused(self, unsafe_monitor):
        """High risk prevents safety-net recovery."""
        meta = _make_metadata(status="paused")

        with (
            patch("src.agent_loop_detection.agent_metadata", {"agent-1": meta}),
            patch("src.agent_loop_detection.monitors", {"agent-1": unsafe_monitor}),
        ):
            from src.agent_loop_detection import _safety_net_resume
            await _safety_net_resume("agent-1", reason="LLM unavailable")

        assert meta.status == "paused", "unsafe agent should stay paused"

    @pytest.mark.asyncio
    async def test_void_active_agent_stays_paused(self, safe_monitor):
        safe_monitor.state.void_active = True
        meta = _make_metadata(status="paused")

        with (
            patch("src.agent_loop_detection.agent_metadata", {"agent-1": meta}),
            patch("src.agent_loop_detection.monitors", {"agent-1": safe_monitor}),
        ):
            from src.agent_loop_detection import _safety_net_resume

            await _safety_net_resume("agent-1", reason="LLM unavailable")

        assert meta.status == "paused"

    @pytest.mark.asyncio
    async def test_already_active_agent_is_noop(self, safe_monitor):
        """If agent is already active, safety net should do nothing."""
        meta = _make_metadata(status="active")

        with (
            patch("src.agent_loop_detection.agent_metadata", {"agent-1": meta}),
            patch("src.agent_loop_detection.monitors", {"agent-1": safe_monitor}),
        ):
            from src.agent_loop_detection import _safety_net_resume
            await _safety_net_resume("agent-1", reason="test")

        assert meta.status == "active"
        assert not hasattr(meta, "_lifecycle_events") or len(meta._lifecycle_events) == 0

    @pytest.mark.asyncio
    async def test_missing_agent_is_noop(self):
        """If agent doesn't exist in metadata, safety net should not crash."""
        with patch("src.agent_loop_detection.agent_metadata", {}):
            from src.agent_loop_detection import _safety_net_resume
            await _safety_net_resume("nonexistent", reason="test")
        # No exception = pass
