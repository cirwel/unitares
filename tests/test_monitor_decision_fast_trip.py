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
