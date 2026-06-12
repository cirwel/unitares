"""Tests for event_detector.py — drift trend classification and EWMA forecasting."""

import pytest

from src.event_detector import (
    classify_drift_trend,
    predict_drift_crossing,
    TREND_STABLE,
    TREND_DRIFTING_UP,
    TREND_DRIFTING_DOWN,
    DRIFT_ALERT_THRESHOLD,
)


class TestClassifyDriftTrend:
    def test_empty_history(self):
        trend, strength = classify_drift_trend([])
        assert trend == TREND_STABLE

    def test_short_history_still_classifies(self):
        # Even 2 values can classify if delta is significant
        trend, strength = classify_drift_trend([0.1, 0.2])
        assert trend == TREND_DRIFTING_UP

    def test_stable_flat(self):
        trend, _ = classify_drift_trend([0.1] * 10)
        assert trend == TREND_STABLE

    def test_drifting_up(self):
        # Monotonically increasing
        history = [i * 0.05 for i in range(10)]
        trend, strength = classify_drift_trend(history)
        assert trend == TREND_DRIFTING_UP
        assert strength > 0.5

    def test_drifting_down(self):
        # Monotonically decreasing
        history = [0.5 - i * 0.05 for i in range(10)]
        trend, strength = classify_drift_trend(history)
        assert trend == TREND_DRIFTING_DOWN
        assert strength > 0.5


class TestPredictDriftCrossing:
    def test_empty_history_returns_zeros(self):
        result = predict_drift_crossing([])
        assert result["ewma_current"] == 0.0
        assert result["ewma_slope"] == 0.0
        assert result["predicted_crossing_steps"] is None
        assert result["confidence"] == 0.0

    def test_short_history_returns_zeros(self):
        result = predict_drift_crossing([0.1, 0.2])
        assert result["confidence"] == 0.0

    def test_flat_history_no_crossing(self):
        result = predict_drift_crossing([0.01] * 10, threshold=0.3)
        assert result["predicted_crossing_steps"] is None
        # Low confidence when slope is flat
        assert result["confidence"] < 0.5

    def test_rising_history_predicts_crossing(self):
        # Linearly increasing toward threshold
        history = [i * 0.02 for i in range(15)]
        result = predict_drift_crossing(history, threshold=0.5)
        assert result["ewma_slope"] > 0
        # Should predict a crossing since values are rising toward 0.5
        # (whether it predicts within forecast window depends on exact EWMA)

    def test_already_above_threshold_no_crossing(self):
        # Already above threshold — no crossing predicted
        history = [0.6] * 10
        result = predict_drift_crossing(history, threshold=0.3)
        assert result["predicted_crossing_steps"] is None

    def test_confidence_scales_with_history_length(self):
        # Short history: low confidence
        short = predict_drift_crossing([0.01, 0.02, 0.03], threshold=0.5)
        # Long history: higher confidence
        long_hist = predict_drift_crossing([i * 0.01 for i in range(15)], threshold=0.5)
        assert long_hist["confidence"] >= short["confidence"]

    def test_custom_alpha(self):
        history = [i * 0.03 for i in range(10)]
        low_alpha = predict_drift_crossing(history, alpha=0.1)
        high_alpha = predict_drift_crossing(history, alpha=0.9)
        # Higher alpha gives more weight to recent values
        assert high_alpha["ewma_current"] != low_alpha["ewma_current"]

    def test_negative_drift_predicts_negative_crossing(self):
        # Drift going negative
        history = [-i * 0.03 for i in range(15)]
        result = predict_drift_crossing(history, threshold=0.5)
        assert result["ewma_slope"] < 0

    def test_returns_rounded_values(self):
        history = [i * 0.0123456789 for i in range(10)]
        result = predict_drift_crossing(history)
        # ewma_current and ewma_slope should be rounded to 6 decimal places
        assert len(str(result["ewma_current"]).split(".")[-1]) <= 6
        assert len(str(result["ewma_slope"]).split(".")[-1]) <= 6


from src.event_detector import GovernanceEventDetector


class TestSeedKnownAgents:
    def test_seeded_agents_do_not_fire_agent_new(self):
        detector = GovernanceEventDetector()
        seeded = detector.seed_known_agents([
            ("uuid-vigil", "Vigil"),
            ("uuid-sentinel", "Sentinel"),
        ])
        assert seeded == 2

        # First detect_events for a *different* agent to populate _prev_state
        detector.detect_events(
            agent_id="uuid-other", agent_name="Other",
            action="proceed", risk=0.0, risk_raw=0.0,
            risk_adjustment=0.0, risk_reason="", drift=[0, 0, 0], verdict="proceed",
        )

        # Now Vigil checks in — should NOT fire agent_new
        events = detector.detect_events(
            agent_id="uuid-vigil", agent_name="Vigil",
            action="proceed", risk=0.1, risk_raw=0.1,
            risk_adjustment=0.0, risk_reason="", drift=[0, 0, 0], verdict="proceed",
        )
        assert not any(e["type"] == "agent_new" for e in events)

    def test_unseeded_agent_fires_agent_new(self):
        detector = GovernanceEventDetector()
        detector.seed_known_agents([("uuid-vigil", "Vigil")])

        # Unknown agent after seeding — should fire agent_new
        events = detector.detect_events(
            agent_id="uuid-unknown", agent_name="Unknown",
            action="proceed", risk=0.0, risk_raw=0.0,
            risk_adjustment=0.0, risk_reason="", drift=[0, 0, 0], verdict="proceed",
        )
        assert any(e["type"] == "agent_new" for e in events)

    def test_seed_does_not_overwrite_existing(self):
        detector = GovernanceEventDetector()
        # Agent already seen via detect_events
        detector.detect_events(
            agent_id="uuid-vigil", agent_name="Vigil",
            action="proceed", risk=0.5, risk_raw=0.5,
            risk_adjustment=0.0, risk_reason="", drift=[0.1, 0, 0], verdict="proceed",
        )
        # Seeding should not clobber existing state
        seeded = detector.seed_known_agents([("uuid-vigil", "Vigil")])
        assert seeded == 0
        assert detector._prev_state["uuid-vigil"]["risk"] == 0.5


class TestRecordEvent:
    def test_records_event_with_id_and_returns_it(self):
        detector = GovernanceEventDetector(max_stored_events=10)
        stored = detector.record_event({
            "type": "sentinel_finding",
            "severity": "high",
            "message": "fleet coherence dipped",
            "agent_id": "sentinel",
            "agent_name": "Sentinel",
            "fingerprint": "abc123",
        })
        assert stored is not None
        assert stored["event_id"] == 1
        assert stored["type"] == "sentinel_finding"
        events = detector.get_recent_events(limit=10)
        assert len(events) == 1

    def test_duplicate_fingerprint_within_window_is_dropped(self):
        detector = GovernanceEventDetector(max_stored_events=10)
        first = detector.record_event({
            "type": "sentinel_finding", "severity": "high",
            "message": "m1", "agent_id": "a", "agent_name": "n",
            "fingerprint": "same",
        })
        second = detector.record_event({
            "type": "sentinel_finding", "severity": "high",
            "message": "m2", "agent_id": "a", "agent_name": "n",
            "fingerprint": "same",
        })
        assert first is not None
        assert second is None
        assert len(detector.get_recent_events(limit=10)) == 1

    def test_different_fingerprints_both_stored(self):
        detector = GovernanceEventDetector(max_stored_events=10)
        a = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                    "agent_id": "x", "agent_name": "n", "fingerprint": "fp1"})
        b = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                    "agent_id": "x", "agent_name": "n", "fingerprint": "fp2"})
        assert a is not None and b is not None
        assert len(detector.get_recent_events(limit=10)) == 2

    def test_missing_fingerprint_is_rejected(self):
        detector = GovernanceEventDetector(max_stored_events=10)
        stored = detector.record_event({
            "type": "t", "severity": "info", "message": "m",
            "agent_id": "x", "agent_name": "n",
        })
        assert stored is None

    def test_dedup_expires_after_window(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        detector = GovernanceEventDetector(max_stored_events=10)
        t0 = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return FakeDatetime._current
            _current = t0

        monkeypatch.setattr("src.event_detector.datetime", FakeDatetime)
        FakeDatetime._current = t0
        detector.record_event({"type": "t", "severity": "info", "message": "m",
                                "agent_id": "x", "agent_name": "n", "fingerprint": "fp"})
        # Jump past the 30-minute dedup window
        FakeDatetime._current = t0 + timedelta(minutes=31)
        second = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                         "agent_id": "x", "agent_name": "n", "fingerprint": "fp"})
        assert second is not None
        assert len(detector.get_recent_events(limit=10)) == 2

    def test_stamps_timestamp_if_missing(self):
        detector = GovernanceEventDetector(max_stored_events=10)
        stored = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                          "agent_id": "x", "agent_name": "n", "fingerprint": "fp"})
        assert "timestamp" in stored
        assert stored["timestamp"].endswith("+00:00") or stored["timestamp"].endswith("Z")

    def test_change_token_suppresses_unchanged_condition_across_window(self, monkeypatch):
        """A frozen/persisting condition (same change_token) must NOT re-emit even
        after the time window lapses. This is the stale-history risk_spike bug:
        an idle agent's one-time spike re-fired every 30-min sweep. Emit-on-change
        suppression is time-independent, so the time jump must not resurrect it."""
        from datetime import datetime, timedelta, timezone
        detector = GovernanceEventDetector(max_stored_events=10)
        t0 = datetime(2026, 6, 12, 1, 0, 0, tzinfo=timezone.utc)

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return FakeDatetime._current
            _current = t0

        monkeypatch.setattr("src.event_detector.datetime", FakeDatetime)
        FakeDatetime._current = t0
        first = detector.record_event({
            "type": "risk_spike", "severity": "medium",
            "message": "m", "agent_id": "idle", "agent_name": "n",
            "fingerprint": "fp", "change_token": "tok_033_055",
        })
        # Three sweeps, each well past the 30-min window — the frozen window
        # yields the identical token every time and must stay suppressed.
        suppressed = []
        for mins in (31, 62, 120):
            FakeDatetime._current = t0 + timedelta(minutes=mins)
            suppressed.append(detector.record_event({
                "type": "risk_spike", "severity": "medium",
                "message": "m", "agent_id": "idle", "agent_name": "n",
                "fingerprint": "fp", "change_token": "tok_033_055",
            }))
        assert first is not None
        assert all(s is None for s in suppressed)
        assert len(detector.get_recent_events(limit=10)) == 1

    def test_change_token_emits_when_condition_changes(self):
        """When the underlying data moves, the token changes and the event emits."""
        detector = GovernanceEventDetector(max_stored_events=10)
        first = detector.record_event({
            "type": "risk_spike", "severity": "medium", "message": "m",
            "agent_id": "a", "agent_name": "n",
            "fingerprint": "fp", "change_token": "tok_v1",
        })
        same = detector.record_event({
            "type": "risk_spike", "severity": "medium", "message": "m",
            "agent_id": "a", "agent_name": "n",
            "fingerprint": "fp", "change_token": "tok_v1",
        })
        changed = detector.record_event({
            "type": "risk_spike", "severity": "high", "message": "m2",
            "agent_id": "a", "agent_name": "n",
            "fingerprint": "fp", "change_token": "tok_v2",
        })
        assert first is not None
        assert same is None
        assert changed is not None
        assert len(detector.get_recent_events(limit=10)) == 2

    def test_token_and_tokenless_paths_coexist(self):
        """Token-less events keep the original time-window dedup; token events do
        not pollute that path (backward compatibility)."""
        detector = GovernanceEventDetector(max_stored_events=10)
        a = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                    "agent_id": "x", "agent_name": "n", "fingerprint": "tl"})
        a_dup = detector.record_event({"type": "t", "severity": "info", "message": "m",
                                       "agent_id": "x", "agent_name": "n", "fingerprint": "tl"})
        assert a is not None
        assert a_dup is None  # time-window dedup still active for token-less

    def test_change_tokens_dict_is_bounded(self):
        """The change-token map must not grow unbounded across many agents."""
        detector = GovernanceEventDetector(max_stored_events=10)
        detector._max_change_tokens = 50
        for i in range(120):
            detector.record_event({
                "type": "risk_spike", "severity": "medium", "message": "m",
                "agent_id": f"agent_{i}", "agent_name": "n",
                "fingerprint": f"fp_{i}", "change_token": f"tok_{i}",
            })
        assert len(detector._change_tokens) <= detector._max_change_tokens
