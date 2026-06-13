"""Tests for the agent profile measurement layer.

Verifies that AgentProfile tracks differentiated per-agent metrics
outside the EISV ODE (update density, complexity, confidence, drift, verdicts).
"""

import time
import pytest
from src.agent_profile import (
    AgentProfile, get_agent_profile, get_all_profiles,
    hydrate_profile, _profiles,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Clear the global registry before each test."""
    _profiles.clear()
    yield
    _profiles.clear()


@pytest.mark.smoke
class TestAgentProfileBasics:
    """Basic recording and metric computation."""

    def test_empty_profile(self):
        p = AgentProfile()
        assert p.total_updates == 0
        assert p.update_density == 0.0
        assert p.session_tenure_hours == 0.0
        assert p.complexity_stats["count"] == 0
        assert p.confidence_stats["count"] == 0
        assert p.drift_stats["count"] == 0
        assert p.verdict_trajectory == {}
        assert p.verdict_trend is None

    def test_single_checkin(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.6, confidence=0.8, verdict="proceed")
        assert p.total_updates == 1
        assert p.complexity_stats["mean"] == 0.6
        assert p.complexity_stats["count"] == 1
        assert p.confidence_stats["mean"] == 0.8
        assert p.verdict_trajectory == {"proceed": 1}

    def test_multiple_checkins_complexity_stats(self):
        p = AgentProfile()
        values = [0.2, 0.4, 0.6, 0.8, 1.0]
        for v in values:
            p.record_checkin(complexity=v)
        stats = p.complexity_stats
        assert stats["count"] == 5
        assert abs(stats["mean"] - 0.6) < 0.001
        assert stats["min"] == 0.2
        assert stats["max"] == 1.0
        assert stats["std"] > 0  # non-zero variance

    def test_confidence_none_skipped(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, confidence=None)
        p.record_checkin(complexity=0.5, confidence=0.7)
        assert p.confidence_stats["count"] == 1
        assert p.confidence_stats["mean"] == 0.7


class TestEthicalDrift:
    """Drift magnitude tracking."""

    def test_zero_drift(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, ethical_drift=[0.0, 0.0, 0.0])
        assert p.drift_stats["mean"] == 0.0
        assert p.drift_stats["max"] == 0.0

    def test_nonzero_drift(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, ethical_drift=[0.3, 0.4, 0.0])
        # magnitude = sqrt(0.09 + 0.16) = 0.5
        assert abs(p.drift_stats["mean"] - 0.5) < 0.001
        assert abs(p.drift_stats["max"] - 0.5) < 0.001

    def test_empty_drift_list_skipped(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, ethical_drift=[])
        assert p.drift_stats["count"] == 0


class TestVerdictTrajectory:
    """Verdict history and trend analysis."""

    def test_verdict_counting(self):
        p = AgentProfile()
        for _ in range(3):
            p.record_checkin(complexity=0.5, verdict="proceed")
        p.record_checkin(complexity=0.5, verdict="guide")
        assert p.verdict_trajectory == {"proceed": 3, "guide": 1}

    def test_trend_insufficient_data(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, verdict="proceed")
        assert p.verdict_trend is None

    def test_trend_stable(self):
        p = AgentProfile()
        for _ in range(12):
            p.record_checkin(complexity=0.5, verdict="proceed")
        assert p.verdict_trend == "stable"

    def test_trend_degrading(self):
        p = AgentProfile()
        for _ in range(6):
            p.record_checkin(complexity=0.5, verdict="proceed")
        for _ in range(6):
            p.record_checkin(complexity=0.5, verdict="pause")
        assert p.verdict_trend == "degrading"

    def test_trend_improving(self):
        p = AgentProfile()
        for _ in range(6):
            p.record_checkin(complexity=0.5, verdict="pause")
        for _ in range(6):
            p.record_checkin(complexity=0.5, verdict="proceed")
        assert p.verdict_trend == "improving"


class TestUpdateDensity:
    """Update frequency measurement."""

    def test_density_with_timestamps(self):
        p = AgentProfile()
        now = time.time()
        # Simulate 10 updates in the last 30 minutes
        for i in range(10):
            p.record_checkin(complexity=0.5, timestamp=now - 1800 + i * 180)
        density = p.update_density
        assert density is not None
        assert density > 0
        # ~10 updates in 0.5 hours = ~20/hr
        assert density > 10

    def test_short_window_does_not_extrapolate(self):
        """Two check-ins 36s apart must NOT project to a ~350/hr rate.

        Regression for the flat-prior overclaim (dogfood 2026-06-13): a tiny
        sample over a few seconds should report None ("not enough window yet"),
        not a per-hour rate extrapolated from the burst.
        """
        p = AgentProfile()
        now = time.time()
        p.record_checkin(complexity=0.5, timestamp=now - 36)
        p.record_checkin(complexity=0.5, timestamp=now)
        assert p.update_density is None

    def test_short_window_summary_carries_honest_note(self):
        p = AgentProfile()
        now = time.time()
        p.record_checkin(complexity=0.5, timestamp=now - 36)
        p.record_checkin(complexity=0.5, timestamp=now)
        summary = p.to_summary()
        assert summary["update_density_per_hour"] is None
        assert "update_density_note" in summary

    def test_long_window_summary_has_no_note(self):
        p = AgentProfile()
        now = time.time()
        for i in range(10):
            p.record_checkin(complexity=0.5, timestamp=now - 1800 + i * 180)
        summary = p.to_summary()
        assert summary["update_density_per_hour"] is not None
        assert "update_density_note" not in summary


class TestSessionTenure:
    """Session duration tracking."""

    def test_tenure_grows(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, timestamp=time.time() - 7200)  # 2 hours ago
        tenure = p.session_tenure_hours
        assert tenure >= 1.9  # approximately 2 hours


class TestSerialization:
    """Round-trip serialization."""

    def test_to_dict_from_dict(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.3, confidence=0.9, ethical_drift=[0.1, 0.2, 0.0], verdict="proceed")
        p.record_checkin(complexity=0.7, confidence=0.6, ethical_drift=[0.0, 0.0, 0.0], verdict="guide")

        d = p.to_dict()
        p2 = AgentProfile.from_dict(d)

        assert p2.total_updates == 2
        assert p2._complexity_count == 2
        assert abs(p2._complexity_mean - p._complexity_mean) < 1e-9
        assert abs(p2._confidence_mean - p._confidence_mean) < 1e-9
        assert list(p2._verdict_history) == list(p._verdict_history)

    def test_to_summary(self):
        p = AgentProfile()
        p.record_checkin(complexity=0.5, confidence=0.8, verdict="proceed")
        summary = p.to_summary()
        assert "total_updates" in summary
        assert "update_density_per_hour" in summary
        assert "complexity" in summary
        assert "confidence" in summary
        assert "drift" in summary
        assert "verdict_trajectory" in summary
        assert "verdict_trend" in summary


class TestGlobalRegistry:
    """Global profile registry."""

    def test_get_creates_new(self):
        p = get_agent_profile("agent-1")
        assert isinstance(p, AgentProfile)
        assert p.total_updates == 0

    def test_get_returns_same(self):
        p1 = get_agent_profile("agent-1")
        p1.record_checkin(complexity=0.5)
        p2 = get_agent_profile("agent-1")
        assert p2.total_updates == 1

    def test_different_agents(self):
        p1 = get_agent_profile("agent-1")
        p2 = get_agent_profile("agent-2")
        p1.record_checkin(complexity=0.3)
        p2.record_checkin(complexity=0.9)
        assert p1.complexity_stats["mean"] == 0.3
        assert p2.complexity_stats["mean"] == 0.9

    def test_get_all_profiles(self):
        get_agent_profile("a")
        get_agent_profile("b")
        all_p = get_all_profiles()
        assert "a" in all_p
        assert "b" in all_p


class TestAgentDifferentiation:
    """The core problem: agents should look different from each other."""

    def test_high_vs_low_complexity_agents(self):
        """Two agents doing different work should have visibly different profiles."""
        simple = AgentProfile()
        complex_ = AgentProfile()

        for _ in range(20):
            simple.record_checkin(complexity=0.2, confidence=0.9, verdict="proceed")
            complex_.record_checkin(complexity=0.8, confidence=0.6, verdict="guide")

        # Complexity means should differ
        assert abs(simple.complexity_stats["mean"] - complex_.complexity_stats["mean"]) > 0.4
        # Confidence means should differ
        assert abs(simple.confidence_stats["mean"] - complex_.confidence_stats["mean"]) > 0.2
        # Verdict trajectories should differ
        assert simple.verdict_trajectory.get("proceed", 0) > complex_.verdict_trajectory.get("proceed", 0)
        assert complex_.verdict_trajectory.get("guide", 0) > simple.verdict_trajectory.get("guide", 0)

    def test_active_vs_idle_agent(self):
        """Active agent should have higher update density."""
        active = AgentProfile()
        idle = AgentProfile()

        now = time.time()
        # Active: 20 updates in last hour
        for i in range(20):
            active.record_checkin(complexity=0.5, timestamp=now - 3600 + i * 180)
        # Idle: 2 updates in last hour
        for i in range(2):
            idle.record_checkin(complexity=0.5, timestamp=now - 3600 + i * 1800)

        assert active.update_density > idle.update_density


class TestProfilePersistence:
    """Hydration and serialization round-trip for restart survival."""

    def test_hydrate_profile_creates_entry(self):
        """hydrate_profile() should restore a profile into the global registry."""
        p = AgentProfile()
        p.record_checkin(complexity=0.7, confidence=0.9, verdict="guide")
        hydrate_profile("test-hydrate", p.to_dict())
        assert "test-hydrate" in _profiles
        assert _profiles["test-hydrate"].total_updates == 1
        assert _profiles["test-hydrate"].complexity_stats["mean"] == pytest.approx(0.7)

    def test_hydrate_profile_skips_empty_and_none(self):
        """hydrate_profile() with empty dict or None should be a no-op."""
        hydrate_profile("test-empty", {})
        hydrate_profile("test-none", None)
        assert "test-empty" not in _profiles
        assert "test-none" not in _profiles

    def test_serialization_roundtrip_preserves_welford(self):
        """to_dict/from_dict should preserve Welford running statistics."""
        p = AgentProfile()
        for i in range(15):
            p.record_checkin(
                complexity=0.1 * (i + 1),
                confidence=0.5 + 0.03 * i,
                ethical_drift=[0.01 * i, 0.02 * i],
                verdict="proceed" if i < 10 else "guide",
            )
        d = p.to_dict()
        restored = AgentProfile.from_dict(d)

        assert restored.total_updates == p.total_updates
        assert restored._complexity_count == p._complexity_count
        assert restored._complexity_mean == pytest.approx(p._complexity_mean)
        assert restored._complexity_m2 == pytest.approx(p._complexity_m2)
        assert restored._confidence_mean == pytest.approx(p._confidence_mean)
        assert restored._drift_mean == pytest.approx(p._drift_mean)
        assert list(restored._verdict_history) == list(p._verdict_history)

    def test_hydrate_then_continue_recording(self):
        """After hydration, continued recording should work correctly."""
        p = AgentProfile()
        for i in range(5):
            p.record_checkin(complexity=0.3, verdict="proceed")

        hydrate_profile("test-continue", p.to_dict())
        restored = _profiles["test-continue"]
        restored.record_checkin(complexity=0.9, verdict="guide")

        assert restored.total_updates == 6
        # Mean should shift toward 0.9 (was 0.3 for 5, now 0.9 for 1)
        assert restored.complexity_stats["mean"] > 0.3
        assert "guide" in restored.verdict_trajectory
