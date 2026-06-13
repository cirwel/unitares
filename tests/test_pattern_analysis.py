"""
Tests for src/pattern_analysis.py - analyze_trend, detect_anomalies, analyze_agent_patterns

Pure function tests with no external dependencies (only numpy).
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock
from collections import deque

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.pattern_analysis import (
    analyze_trend,
    detect_anomalies_in_history,
    analyze_agent_patterns,
)


# --- analyze_trend Tests ---


class TestAnalyzeTrend:
    """Tests for analyze_trend() function."""

    def test_empty_list(self):
        assert analyze_trend([]) == "stable"

    def test_single_value(self):
        assert analyze_trend([0.5]) == "stable"

    def test_constant_values(self):
        assert analyze_trend([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]) == "stable"

    def test_increasing_trend(self):
        values = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55]
        assert analyze_trend(values) == "increasing"

    def test_decreasing_trend(self):
        values = [0.55, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1]
        assert analyze_trend(values) == "decreasing"

    def test_small_change_is_stable(self):
        """Changes below 5% threshold should be 'stable'."""
        values = [0.50, 0.50, 0.50, 0.50, 0.50, 0.51, 0.51, 0.51, 0.51, 0.51]
        assert analyze_trend(values) == "stable"

    def test_clear_trend_with_enough_data(self):
        """Need 2*window values for proper trend comparison."""
        # Default window=5, so need 10+ values
        assert analyze_trend([0.1]*5 + [0.5]*5) == "increasing"
        assert analyze_trend([0.5]*5 + [0.1]*5) == "decreasing"

    def test_custom_window(self):
        values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        result = analyze_trend(values, window=3)
        assert result == "increasing"

    def test_short_list_adjusts_window(self):
        """Window should adjust down for short lists."""
        result = analyze_trend([0.2, 0.3, 0.8], window=10)
        assert result in ("increasing", "stable")  # Depends on window adjustment


# --- detect_anomalies_in_history Tests ---


class TestDetectAnomalies:
    """Tests for detect_anomalies_in_history()."""

    def test_empty_history(self):
        assert detect_anomalies_in_history([], [], []) == []

    def test_short_history_no_anomalies(self):
        assert detect_anomalies_in_history([0.3, 0.3], [0.5, 0.5], ["t1", "t2"]) == []

    def test_risk_spike_detected(self):
        """15%+ increase in recent risk should be detected."""
        risk = [0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.5, 0.5]
        coherence = [0.5] * 8
        timestamps = [f"t{i}" for i in range(8)]

        anomalies = detect_anomalies_in_history(risk, coherence, timestamps)

        risk_spikes = [a for a in anomalies if a["type"] == "risk_spike"]
        assert len(risk_spikes) >= 1
        assert risk_spikes[0]["severity"] in ("medium", "high")

    def test_high_severity_risk_spike(self):
        """25%+ risk change should be high severity."""
        risk = [0.1, 0.1, 0.1, 0.1, 0.1, 0.6, 0.6, 0.6]
        coherence = [0.5] * 8
        timestamps = [f"t{i}" for i in range(8)]

        anomalies = detect_anomalies_in_history(risk, coherence, timestamps)

        risk_spikes = [a for a in anomalies if a["type"] == "risk_spike"]
        assert len(risk_spikes) >= 1
        assert risk_spikes[0]["severity"] == "high"

    def test_no_risk_spike_when_stable(self):
        risk = [0.3, 0.3, 0.3, 0.3, 0.3, 0.3]
        coherence = [0.5] * 6
        timestamps = [f"t{i}" for i in range(6)]

        anomalies = detect_anomalies_in_history(risk, coherence, timestamps)
        risk_spikes = [a for a in anomalies if a["type"] == "risk_spike"]
        assert len(risk_spikes) == 0

    def test_coherence_drop_detected(self):
        """5%+ coherence drop should be detected."""
        risk = [0.3] * 8
        coherence = [0.8, 0.8, 0.8, 0.8, 0.8, 0.6, 0.6, 0.6]
        timestamps = [f"t{i}" for i in range(8)]

        anomalies = detect_anomalies_in_history(risk, coherence, timestamps)

        drops = [a for a in anomalies if a["type"] == "coherence_drop"]
        assert len(drops) >= 1

    def test_anomaly_includes_context(self):
        """Anomalies should include context dict."""
        risk = [0.1, 0.1, 0.1, 0.1, 0.1, 0.5, 0.5, 0.5]
        coherence = [0.5] * 8
        timestamps = [f"t{i}" for i in range(8)]

        anomalies = detect_anomalies_in_history(risk, coherence, timestamps)

        for a in anomalies:
            assert "type" in a
            assert "severity" in a
            assert "description" in a
            assert "context" in a

    def test_empty_timestamps_handled(self):
        """Should handle empty timestamp list gracefully."""
        risk = [0.1, 0.1, 0.1, 0.5, 0.5, 0.5]
        coherence = [0.5] * 6

        anomalies = detect_anomalies_in_history(risk, coherence, [])
        # Should not crash; timestamp might be None
        for a in anomalies:
            assert a["timestamp"] is None


# --- analyze_agent_patterns Tests ---


class TestAnalyzeAgentPatterns:
    """Tests for analyze_agent_patterns()."""

    def _make_mock_monitor(self, **kwargs):
        """Create a mock monitor with a state object."""
        defaults = {
            "E": 0.7, "I": 0.8, "S": 0.3, "V": 0.2,
            "coherence": 0.52, "lambda1": 0.0,
            "update_count": 10,
            "risk_history": [0.3, 0.3, 0.3, 0.3, 0.3],
            "coherence_history": [0.5, 0.5, 0.5, 0.5, 0.5],
            "E_history": [0.7, 0.7, 0.7, 0.7, 0.7],
            "I_history": [0.8, 0.8, 0.8, 0.8, 0.8],
            "S_history": [0.3, 0.3, 0.3, 0.3, 0.3],
            "V_history": [0.2, 0.2, 0.2, 0.2, 0.2],
            "timestamp_history": [f"t{i}" for i in range(5)],
            "decision_history": ["proceed", "proceed", "proceed", "pause", "proceed"],
        }
        defaults.update(kwargs)

        state = MagicMock()
        for key, value in defaults.items():
            setattr(state, key, value)

        monitor = MagicMock()
        monitor.state = state
        return monitor

    def test_returns_current_state(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor)

        assert "current_state" in result
        cs = result["current_state"]
        assert cs["E"] == 0.7
        assert cs["I"] == 0.8
        assert cs["S"] == 0.3
        assert cs["V"] == 0.2
        assert cs["coherence"] == 0.52

    def test_returns_patterns(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor)

        assert "patterns" in result
        assert "risk_trend" in result["patterns"]
        assert "coherence_trend" in result["patterns"]
        assert "trend" in result["patterns"]

    def test_stable_patterns(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor)
        assert result["patterns"]["trend"] == "stable"

    def test_improving_trend(self):
        monitor = self._make_mock_monitor(
            risk_history=[0.5, 0.5, 0.5, 0.5, 0.5, 0.3, 0.3, 0.3, 0.3, 0.3],
            coherence_history=[0.4, 0.4, 0.4, 0.4, 0.4, 0.6, 0.6, 0.6, 0.6, 0.6],
        )
        result = analyze_agent_patterns(monitor)
        assert result["patterns"]["trend"] == "improving"

    def test_degrading_trend(self):
        monitor = self._make_mock_monitor(
            risk_history=[0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.5, 0.5, 0.5, 0.5],
            coherence_history=[0.6, 0.6, 0.6, 0.6, 0.6, 0.4, 0.4, 0.4, 0.4, 0.4],
        )
        result = analyze_agent_patterns(monitor)
        assert result["patterns"]["trend"] == "degrading"

    def test_returns_anomalies(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor)
        assert "anomalies" in result
        assert isinstance(result["anomalies"], list)

    def test_returns_summary(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor)

        assert "summary" in result
        assert result["summary"]["total_updates"] == 10
        assert "mean_risk" in result["summary"]
        assert "decision_distribution" in result["summary"]

    def test_decision_distribution(self):
        monitor = self._make_mock_monitor(
            decision_history=["proceed", "proceed", "pause", "approve", "reject"]
        )
        result = analyze_agent_patterns(monitor)
        dist = result["summary"]["decision_distribution"]
        # "proceed" counts: proceed(2) + approve(1) + reflect(0) + revise(0) = 3
        assert dist["proceed"] >= 2
        assert dist["pause"] >= 1  # pause(1) + reject(1) = 2

    def test_includes_recent_history(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor, include_history=True)
        assert "recent_history" in result
        assert "risk_history" in result["recent_history"]

    def test_excludes_history_when_requested(self):
        monitor = self._make_mock_monitor()
        result = analyze_agent_patterns(monitor, include_history=False)
        assert "recent_history" not in result

    def test_empty_history(self):
        monitor = self._make_mock_monitor(
            risk_history=[], coherence_history=[],
            E_history=[], I_history=[], S_history=[], V_history=[],
            timestamp_history=[]
        )
        result = analyze_agent_patterns(monitor)
        assert result["patterns"]["risk_trend"] == "stable"
        assert result["patterns"]["coherence_trend"] == "stable"
        assert result["summary"]["mean_risk"] == 0.0


# --- Detector-layer freshness guard (#637) ---


SPIKE_RISK = [0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.5, 0.5]
FLAT_COHERENCE = [0.5] * 8
DROP_COHERENCE = [0.8, 0.8, 0.8, 0.8, 0.8, 0.6, 0.6, 0.6]


class TestFrozenWindowGuard:
    """detect_anomalies_in_history must not present an anomaly recomputed
    from a frozen history window as a current finding (#637). The guard
    annotates with `stale` — keyed on data advancement (the newest analyzed
    sample), never wall-clock — and never drops anomalies, so reads stay
    non-destructive and consumer-order-independent."""

    def _ts(self, n):
        return [f"t{i}" for i in range(n)]

    def test_frozen_window_marked_stale_on_re_evaluation(self):
        emitted = {}
        first = detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, self._ts(8), emitted_windows=emitted)
        assert [a["type"] for a in first] == ["risk_spike"]
        assert first[0]["stale"] is False

        # Identical (frozen) window: same spike still returned, marked stale.
        second = detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, self._ts(8), emitted_windows=emitted)
        assert [a["type"] for a in second] == ["risk_spike"]
        assert second[0]["stale"] is True

    def test_reads_are_non_destructive_across_consumers(self):
        # A second consumer evaluating the same frozen window still receives
        # the anomaly (only the freshness label differs) — no consumer can
        # make the finding invisible to another.
        emitted = {}
        for _ in range(3):
            result = detect_anomalies_in_history(
                SPIKE_RISK, FLAT_COHERENCE, self._ts(8),
                emitted_windows=emitted)
            assert [a["type"] for a in result] == ["risk_spike"]
            assert result[0]["description"]

    def test_window_advance_refreshes(self):
        emitted = {}
        detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, self._ts(8), emitted_windows=emitted)

        # A new sample lands and the spike pattern persists: that is a
        # current finding again, not a stale one.
        advanced = detect_anomalies_in_history(
            SPIKE_RISK + [0.5], FLAT_COHERENCE + [0.5], self._ts(9),
            emitted_windows=emitted)
        assert advanced[0]["stale"] is False
        assert advanced[0]["timestamp"] == "t8"

    def test_recovery_then_respike_is_fresh(self):
        emitted = {}
        detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, self._ts(8), emitted_windows=emitted)

        # Risk recovers (no anomaly), then spikes again later — the new
        # spike has a new newest-sample key and must come back fresh.
        recovered = SPIKE_RISK + [0.2, 0.2, 0.2, 0.2]
        assert detect_anomalies_in_history(
            recovered, FLAT_COHERENCE + [0.5] * 4, self._ts(12),
            emitted_windows=emitted) == []
        respiked = recovered + [0.6, 0.6, 0.6]
        again = detect_anomalies_in_history(
            respiked, FLAT_COHERENCE + [0.5] * 7, self._ts(15),
            emitted_windows=emitted)
        assert again[0]["type"] == "risk_spike"
        assert again[0]["stale"] is False

    def test_stateless_caller_keeps_legacy_behavior(self):
        # No emitted_windows: pure function, identical output, no stale field.
        for _ in range(2):
            anomalies = detect_anomalies_in_history(
                SPIKE_RISK, FLAT_COHERENCE, self._ts(8))
            assert [a["type"] for a in anomalies] == ["risk_spike"]
            assert "stale" not in anomalies[0]

    def test_anomaly_types_tracked_independently(self):
        emitted = {}
        first = detect_anomalies_in_history(
            SPIKE_RISK, DROP_COHERENCE, self._ts(8), emitted_windows=emitted)
        assert {a["type"]: a["stale"] for a in first} == {
            "risk_spike": False, "coherence_drop": False}

        second = detect_anomalies_in_history(
            SPIKE_RISK, DROP_COHERENCE, self._ts(8), emitted_windows=emitted)
        assert {a["type"]: a["stale"] for a in second} == {
            "risk_spike": True, "coherence_drop": True}

    def test_empty_timestamps_fallback_key(self):
        emitted = {}
        first = detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, [], emitted_windows=emitted)
        assert first[0]["stale"] is False
        second = detect_anomalies_in_history(
            SPIKE_RISK, FLAT_COHERENCE, [], emitted_windows=emitted)
        assert second[0]["stale"] is True

        # Advancement is still detectable without timestamps (length+tail).
        advanced = detect_anomalies_in_history(
            SPIKE_RISK + [0.5], FLAT_COHERENCE + [0.5], [],
            emitted_windows=emitted)
        assert advanced[0]["stale"] is False

    def test_fallback_key_detects_tail_change_at_constant_length(self):
        # Trim-cap regression: history length stays constant (window slides)
        # and the newest value repeats the evicted one's — the tail of the
        # detector's window still differs, so the key must advance.
        emitted = {}
        window = [0.2, 0.2, 0.2, 0.3, 0.5, 0.5]
        detect_anomalies_in_history(
            window, [0.5] * 6, [], emitted_windows=emitted)
        slid = window[1:] + [0.5]  # same length, same last value, new sample
        result = detect_anomalies_in_history(
            slid, [0.5] * 6, [], emitted_windows=emitted)
        assert result[0]["stale"] is False


class TestFrozenWindowGuardViaMonitor:
    """analyze_agent_patterns wires the guard to a per-monitor registry, so
    every consumer (observe, detect_anomalies) shares staleness tracking."""

    def _make_monitor(self):
        state = MagicMock()
        state.E, state.I, state.S, state.V = 0.7, 0.8, 0.3, 0.2
        state.coherence, state.lambda1 = 0.52, 0.0
        state.update_count = 10
        state.risk_history = list(SPIKE_RISK)
        state.coherence_history = list(FLAT_COHERENCE)
        state.E_history = [0.7] * 8
        state.I_history = [0.8] * 8
        state.S_history = [0.3] * 8
        state.V_history = [0.2] * 8
        state.timestamp_history = [f"t{i}" for i in range(8)]
        state.decision_history = ["proceed"] * 8
        state.verdict_history = ["safe"] * 8
        monitor = MagicMock()
        monitor.state = state
        return monitor

    def test_idle_monitor_anomaly_goes_stale(self):
        monitor = self._make_monitor()
        first = analyze_agent_patterns(monitor)["anomalies"]
        assert [(a["type"], a["stale"]) for a in first] == [("risk_spike", False)]

        # Idle agent: no new samples between polls — spike still visible,
        # now labeled stale so no consumer presents it as a current finding.
        second = analyze_agent_patterns(monitor)["anomalies"]
        assert [(a["type"], a["stale"]) for a in second] == [("risk_spike", True)]

    def test_new_checkin_refreshes_persisting_spike(self):
        monitor = self._make_monitor()
        analyze_agent_patterns(monitor)
        assert analyze_agent_patterns(monitor)["anomalies"][0]["stale"] is True

        # A new check-in lands while the spike persists.
        monitor.state.risk_history.append(0.5)
        monitor.state.coherence_history.append(0.5)
        monitor.state.timestamp_history.append("t8")
        result = analyze_agent_patterns(monitor)["anomalies"]
        assert [(a["type"], a["stale"]) for a in result] == [("risk_spike", False)]

    def test_distinct_monitors_do_not_share_staleness(self):
        a, b = self._make_monitor(), self._make_monitor()
        assert analyze_agent_patterns(a)["anomalies"][0]["stale"] is False
        assert analyze_agent_patterns(b)["anomalies"][0]["stale"] is False
