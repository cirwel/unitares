"""Tests for agent_behavioral_baseline.py — Welford's online stats and anomaly detection."""

import math
import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from src.agent_behavioral_baseline import (
    WelfordStats,
    AgentBehavioralBaseline,
    get_agent_behavioral_baseline,
    compute_anomaly_entropy,
    schedule_baseline_save,
    _baselines,
    _save_tasks,
)


# ══════════════════════════════════════════════════
#  WelfordStats
# ══════════════════════════════════════════════════

class TestWelfordStats:
    def test_empty_stats(self):
        s = WelfordStats()
        assert s.count == 0
        assert s.mean == 0.0
        assert s.variance == 0.0
        assert s.std == 0.0

    def test_single_value(self):
        s = WelfordStats()
        s.update(5.0)
        assert s.count == 1
        assert s.mean == 5.0
        assert s.variance == 0.0  # Need at least 2 for variance

    def test_known_sequence(self):
        """Mean and variance match expected values for [1, 2, 3, 4, 5]."""
        s = WelfordStats()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            s.update(v)
        assert s.count == 5
        assert abs(s.mean - 3.0) < 1e-10
        # Sample variance of [1,2,3,4,5] = 2.5
        assert abs(s.variance - 2.5) < 1e-10
        assert abs(s.std - math.sqrt(2.5)) < 1e-10

    def test_z_score_insufficient_data(self):
        s = WelfordStats()
        for v in [1.0, 2.0, 3.0]:
            s.update(v)
        assert s.z_score(10.0) == 0.0  # < 5 samples

    def test_z_score_with_data(self):
        s = WelfordStats()
        for v in [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]:
            s.update(v)
        # All same value → std ≈ 0 → z_score returns 0.0
        assert s.z_score(10.0) == 0.0

    def test_z_score_detects_outlier(self):
        s = WelfordStats()
        for v in [10.0, 10.5, 9.5, 10.2, 9.8, 10.1]:
            s.update(v)
        # Value far from mean should have high z-score
        z = s.z_score(15.0)
        assert abs(z) > 2.0

    def test_roundtrip_serialization(self):
        s = WelfordStats()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            s.update(v)
        d = s.to_dict()
        s2 = WelfordStats.from_dict(d)
        assert s2.count == s.count
        assert abs(s2.mean - s.mean) < 1e-10
        assert abs(s2.variance - s.variance) < 1e-10


# ══════════════════════════════════════════════════
#  AgentBehavioralBaseline
# ══════════════════════════════════════════════════

class TestAgentBehavioralBaseline:
    def test_initial_state(self):
        b = AgentBehavioralBaseline()
        assert b.sample_count == 0
        for signal in AgentBehavioralBaseline.TRACKED_SIGNALS:
            assert b.z_score(signal, 0.5) == 0.0  # No data yet

    def test_update_and_z_score(self):
        b = AgentBehavioralBaseline()
        # Build baseline with consistent values
        for _ in range(10):
            b.update("coherence", 0.5)
        # z-score of the mean should be near 0
        # But since all values are the same, std ≈ 0, so z_score returns 0
        assert b.z_score("coherence", 0.5) == 0.0

    def test_anomaly_detection(self):
        b = AgentBehavioralBaseline()
        # Build baseline with some variance
        for v in [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]:
            b.update("coherence", v)
        # Normal value should not be anomalous
        assert not b.is_anomalous("coherence", 0.51)
        # Extreme value should be anomalous
        assert b.is_anomalous("coherence", 0.9, threshold=2.0)

    def test_unknown_signal_returns_zero(self):
        b = AgentBehavioralBaseline()
        assert b.z_score("nonexistent", 1.0) == 0.0
        assert not b.is_anomalous("nonexistent", 1.0)

    def test_tracked_signals(self):
        b = AgentBehavioralBaseline()
        assert "tool_error_rate" in b.TRACKED_SIGNALS
        assert "tool_call_velocity" in b.TRACKED_SIGNALS
        assert "complexity_divergence" in b.TRACKED_SIGNALS
        assert "coherence" in b.TRACKED_SIGNALS

    def test_roundtrip_serialization(self):
        b = AgentBehavioralBaseline()
        for _ in range(5):
            b.update("coherence", 0.5)
            b.update("tool_error_rate", 0.1)
        d = b.to_dict()
        b2 = AgentBehavioralBaseline.from_dict(d)
        assert b2._stats["coherence"].count == 5
        assert abs(b2._stats["coherence"].mean - 0.5) < 1e-10


# ══════════════════════════════════════════════════
#  compute_anomaly_entropy
# ══════════════════════════════════════════════════

class TestComputeAnomalyEntropy:
    def test_no_data_returns_zero(self):
        b = AgentBehavioralBaseline()
        assert compute_anomaly_entropy(b, {"coherence": 0.5}) == 0.0

    def test_normal_signals_no_penalty(self):
        b = AgentBehavioralBaseline()
        for v in [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]:
            b.update("coherence", v)
        penalty = compute_anomaly_entropy(b, {"coherence": 0.51})
        assert penalty == 0.0

    def test_anomalous_signal_adds_penalty(self):
        b = AgentBehavioralBaseline()
        for v in [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]:
            b.update("coherence", v)
        penalty = compute_anomaly_entropy(b, {"coherence": 0.9})
        assert penalty == 0.05  # One anomaly at default penalty

    def test_multiple_anomalies_stack(self):
        b = AgentBehavioralBaseline()
        for _ in range(10):
            b.update("coherence", 0.5)
            b.update("tool_error_rate", 0.1)
        penalty = compute_anomaly_entropy(b, {
            "coherence": 0.9,        # Anomalous
            "tool_error_rate": 0.8,   # Anomalous
        })
        # With all-same values, std ≈ 0 → z_score returns 0 → no anomaly
        # Need variance for detection
        assert penalty == 0.0  # All-same values = zero variance

    def test_multiple_anomalies_with_variance(self):
        b = AgentBehavioralBaseline()
        for v in [0.1, 0.12, 0.09, 0.11, 0.10, 0.13, 0.08]:
            b.update("coherence", v + 0.4)
            b.update("tool_error_rate", v)
        penalty = compute_anomaly_entropy(b, {
            "coherence": 0.9,        # Far from ~0.5
            "tool_error_rate": 0.8,  # Far from ~0.1
        })
        assert penalty == 0.10  # Two anomalies × 0.05

    def test_none_values_skipped(self):
        b = AgentBehavioralBaseline()
        for v in [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]:
            b.update("coherence", v)
        penalty = compute_anomaly_entropy(b, {"coherence": None})
        assert penalty == 0.0

    def test_custom_threshold_and_penalty(self):
        b = AgentBehavioralBaseline()
        for v in [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]:
            b.update("coherence", v)
        # Lower threshold, higher penalty
        penalty = compute_anomaly_entropy(
            b, {"coherence": 0.9}, threshold=1.0, penalty_per_anomaly=0.1
        )
        assert penalty == 0.1


# ══════════════════════════════════════════════════
#  Global registry
# ══════════════════════════════════════════════════

class TestGlobalRegistry:
    def setup_method(self):
        _baselines.clear()

    def test_get_creates_new(self):
        b = get_agent_behavioral_baseline("agent-1")
        assert isinstance(b, AgentBehavioralBaseline)
        assert b.sample_count == 0

    def test_get_returns_same_instance(self):
        b1 = get_agent_behavioral_baseline("agent-1")
        b2 = get_agent_behavioral_baseline("agent-1")
        assert b1 is b2

    def test_different_agents_different_baselines(self):
        b1 = get_agent_behavioral_baseline("agent-1")
        b2 = get_agent_behavioral_baseline("agent-2")
        assert b1 is not b2
        b1.update("coherence", 0.9)
        assert b2._stats["coherence"].count == 0


# ══════════════════════════════════════════════════
#  schedule_baseline_save — fire-and-forget GC safety
# ══════════════════════════════════════════════════

class TestScheduleBaselineSave:
    def setup_method(self):
        _baselines.clear()
        _save_tasks.clear()

    def test_no_baseline_is_noop(self):
        # No baseline registered → nothing scheduled, no error.
        schedule_baseline_save("unknown-agent")
        assert len(_save_tasks) == 0

    def test_no_event_loop_is_noop(self):
        # Called from sync context (no running loop) → skip silently.
        get_agent_behavioral_baseline("agent-1")
        schedule_baseline_save("agent-1")  # not inside a loop
        assert len(_save_tasks) == 0

    @pytest.mark.asyncio
    async def test_task_ref_held_until_done(self):
        get_agent_behavioral_baseline("agent-1")
        mock_db = MagicMock()
        mock_db.save_behavioral_baseline = AsyncMock()
        with patch("src.db.get_db", return_value=mock_db):
            schedule_baseline_save("agent-1")
            # Strong ref is held while the save is in flight (guards against the
            # event loop's weak-ref GC dropping the task mid-await).
            assert len(_save_tasks) == 1
            task = next(iter(_save_tasks))
            await task
            # Done-callback discards the ref so the set does not leak.
            assert len(_save_tasks) == 0
        mock_db.save_behavioral_baseline.assert_awaited_once()
