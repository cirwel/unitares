"""Unit tests for the F2 latest-risk fast-trip in monitor_decision.

The gate runs on the (possibly task-adjusted) risk_score. When the latest raw
risk observation spiked past the pause threshold but the gated value cleared, a
clean ``approve`` must be upgraded to ``guide`` so the spike is not silently
approved. A genuine pause is never weakened.
"""

from types import SimpleNamespace

from config.governance_config import config
from src.monitor_decision import _maybe_latest_risk_fast_trip, make_decision


def _healthy_state(risk_history):
    return SimpleNamespace(
        E=0.80, I=0.85, S=0.15, V=0.05,
        coherence=0.90,
        void_active=False,
        coherence_history=[0.90, 0.90, 0.90],
        risk_history=list(risk_history),
    )


class TestFastTripHelper:
    def test_approve_upgraded_to_guide_on_latest_spike(self):
        state = _healthy_state([0.10, 0.72])
        decision = {"action": "proceed", "sub_action": "approve"}
        out = _maybe_latest_risk_fast_trip(state, decision, gated_risk=0.20)
        assert out["sub_action"] == "guide"
        assert out["action"] == "proceed"
        assert out["latest_risk_fast_trip"]["latest_risk"] == 0.72
        assert out["latest_risk_fast_trip"]["gated_risk"] == 0.20

    def test_no_trip_when_latest_below_threshold(self):
        state = _healthy_state([0.10, 0.30])
        decision = {"action": "proceed", "sub_action": "approve"}
        out = _maybe_latest_risk_fast_trip(state, decision, gated_risk=0.20)
        assert out["sub_action"] == "approve"
        assert "latest_risk_fast_trip" not in out

    def test_pause_never_weakened(self):
        state = _healthy_state([0.10, 0.95])
        decision = {"action": "pause", "sub_action": "risk_pause"}
        out = _maybe_latest_risk_fast_trip(state, decision, gated_risk=0.20)
        assert out["action"] == "pause"
        assert out["sub_action"] == "risk_pause"

    def test_existing_guide_left_intact(self):
        state = _healthy_state([0.10, 0.72])
        decision = {"action": "proceed", "sub_action": "guide", "reason": "orig"}
        out = _maybe_latest_risk_fast_trip(state, decision, gated_risk=0.50)
        # Already a guide — not re-stamped by the fast-trip.
        assert out["sub_action"] == "guide"
        assert out["reason"] == "orig"
        assert "latest_risk_fast_trip" not in out

    def test_no_trip_when_latest_equals_gated(self):
        # When the gate already saw the latest value (no adjustment gap), the
        # normal band logic owns the decision; the fast-trip must not double-fire.
        state = _healthy_state([0.72])
        decision = {"action": "proceed", "sub_action": "approve"}
        out = _maybe_latest_risk_fast_trip(state, decision, gated_risk=0.72)
        assert out["sub_action"] == "approve"


class TestMakeDecisionIntegration:
    def test_low_gated_risk_high_latest_yields_guide(self):
        # gated risk well under the approve threshold -> config returns approve;
        # latest raw observation is above the pause threshold -> fast-trip guides.
        state = _healthy_state([0.10, 0.72])
        decision = make_decision(state, risk_score=0.20, unitares_verdict="safe")
        assert decision["action"] == "proceed"
        assert decision["sub_action"] == "guide"
        assert "latest_risk_fast_trip" in decision

    def test_clean_low_risk_still_approves(self):
        state = _healthy_state([0.10, 0.12])
        decision = make_decision(state, risk_score=0.12, unitares_verdict="safe")
        assert decision["action"] == "proceed"
        assert decision["sub_action"] != "guide" or "latest_risk_fast_trip" not in decision
        assert "latest_risk_fast_trip" not in decision


class TestFastTripAuthorityGate:
    """Contract row 52: every risk_history entry is Φ-derived. When the
    behavioral assessment owns the verdict, Φ is telemetry and must not move a
    warmed agent's action through the fast-trip. Φ cold start owns its verdict
    and keeps the trip."""

    def test_behavioral_authority_skips_the_fast_trip(self):
        state = _healthy_state([0.10, 0.72])
        decision = make_decision(
            state, risk_score=0.20, unitares_verdict="safe",
            latest_risk_fast_trip=False,
        )
        assert decision["action"] == "proceed"
        assert decision["sub_action"] == "approve"
        assert "latest_risk_fast_trip" not in decision

    def test_phi_cold_start_still_fires_the_fast_trip(self):
        state = _healthy_state([0.10, 0.72])
        decision = make_decision(
            state, risk_score=0.20, unitares_verdict="safe",
            latest_risk_fast_trip=True,
        )
        assert decision["sub_action"] == "guide"
        assert "latest_risk_fast_trip" in decision

    def test_skipping_the_trip_never_weakens_a_pause(self):
        state = _healthy_state([0.10, 0.95])
        decision = make_decision(
            state, risk_score=0.20, unitares_verdict="high-risk",
            latest_risk_fast_trip=False,
        )
        assert decision["action"] == "pause"


def _drive_monitor(monkeypatch, driver):
    """Run one real process_update with the verdict driver forced, and return
    the kwargs the monitor passed to make_decision."""
    import uuid

    import numpy as np

    import src.cold_start_risk_confirmation as crc
    from src.governance_monitor import UNITARESMonitor

    monkeypatch.setattr(crc, "classify_verdict_driver", lambda **_: driver)
    monitor = UNITARESMonitor(f"test-fast-trip-gate-{uuid.uuid4().hex[:12]}", load_state=False)
    seen = {}
    real = monitor.make_decision

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(monitor, "make_decision", spy)
    result = monitor.process_update(
        {"parameters": np.array([]), "ethical_drift": [0.0, 0.0, 0.0],
         "response_text": "Ran the focused tests; they pass.", "complexity": 0.5},
        confidence=0.7,
    )
    return seen, result, monitor


def test_process_update_disables_the_trip_under_behavioral_authority(monkeypatch):
    seen, _, _ = _drive_monitor(monkeypatch, "behavioral_assessment")
    assert seen["latest_risk_fast_trip"] is False


def test_process_update_keeps_the_trip_in_phi_cold_start(monkeypatch):
    seen, _, _ = _drive_monitor(monkeypatch, "phi_cold_start")
    assert seen["latest_risk_fast_trip"] is True


def test_process_update_keeps_the_trip_on_the_phi_floor(monkeypatch):
    # PHI_TELEMETRY_ONLY off: Φ still owns a warmed agent's verdict.
    seen, _, _ = _drive_monitor(monkeypatch, "phi_floor")
    assert seen["latest_risk_fast_trip"] is True
