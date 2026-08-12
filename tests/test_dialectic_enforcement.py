"""Tests for dialectic condition enforcement."""

import pytest
from datetime import datetime, timezone, timedelta

from src.mcp_handlers.dialectic.enforcement import (
    enforce_post_ode_conditions,
    enforce_complexity_limit,
    _is_expired,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_decision(action="proceed", reason="all good"):
    return {"action": action, "reason": reason}


def _make_metrics(risk_score=0.3, coherence=0.5):
    return {"risk_score": risk_score, "coherence": coherence, "E": 0.7, "I": 0.6, "S": 0.3, "V": 0.1}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ── risk_target tests ────────────────────────────────────────────────────

class TestRiskTarget:
    def test_escalates_proceed_to_guide(self):
        conditions = [{"type": "risk_target", "value": 0.4, "applied_at": _now_iso()}]
        metrics = _make_metrics(risk_score=0.55)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "guide"
        assert result["dialectic_escalated"] is True
        assert len(warnings) == 1
        assert "risk" in warnings[0]

    def test_no_escalation_when_below_target(self):
        conditions = [{"type": "risk_target", "value": 0.6, "applied_at": _now_iso()}]
        metrics = _make_metrics(risk_score=0.3)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert result is decision  # Same object, not copied
        assert len(warnings) == 0

    def test_severe_overshoot_escalates_to_pause(self):
        conditions = [{"type": "risk_target", "value": 0.3, "applied_at": _now_iso()}]
        # Overshoot > 0.3 * 0.5 = 0.15, so risk > 0.45
        metrics = _make_metrics(risk_score=0.6)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "pause"

    def test_does_not_downgrade_existing_pause(self):
        conditions = [{"type": "risk_target", "value": 0.4, "applied_at": _now_iso()}]
        metrics = _make_metrics(risk_score=0.5)
        decision = _make_decision("pause")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "pause"  # Still pause, not downgraded to guide


# ── coherence_target tests ───────────────────────────────────────────────

class TestCoherenceTarget:
    def test_low_value_is_retired_without_escalation(self):
        conditions = [{"type": "coherence_target", "value": 0.5, "applied_at": _now_iso()}]
        metrics = _make_metrics(coherence=0.42)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert result is decision
        assert len(warnings) == 1
        assert "retired" in warnings[0]
        assert conditions[0]["expired"] is True
        assert conditions[0]["retired_reason"] == "coherence_producer_unprovenanced"

    def test_above_target_is_also_retired(self):
        conditions = [{"type": "coherence_target", "value": 0.4, "applied_at": _now_iso()}]
        metrics = _make_metrics(coherence=0.5)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert len(warnings) == 1
        assert conditions[0]["retired"] is True

    def test_severe_deficit_cannot_pause(self):
        conditions = [{"type": "coherence_target", "value": 0.6, "applied_at": _now_iso()}]
        # Deficit > 0.6 * 0.5 = 0.3, so coherence < 0.3
        metrics = _make_metrics(coherence=0.2)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert len(warnings) == 1

        # Retirement is sticky, so subsequent check-ins do not repeat warnings.
        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)
        assert result["action"] == "proceed"
        assert warnings == []


# ── monitoring_duration / expiry tests ───────────────────────────────────

class TestMonitoringDuration:
    def test_expired_condition_is_skipped(self):
        conditions = [
            {
                "type": "risk_target",
                "value": 0.3,
                "applied_at": _hours_ago(25),
                "duration_hours": 24,
            }
        ]
        metrics = _make_metrics(risk_score=0.5)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert len(warnings) == 0
        assert conditions[0].get("expired") is True

    def test_active_condition_within_duration(self):
        conditions = [
            {
                "type": "risk_target",
                "value": 0.4,
                "applied_at": _hours_ago(12),
                "duration_hours": 24,
            }
        ]
        # risk 0.5, target 0.4: overshoot 0.1, threshold 0.4*0.5=0.2 → guide (not pause)
        metrics = _make_metrics(risk_score=0.5)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "guide"
        assert len(warnings) == 1

    def test_monitoring_duration_propagates_to_siblings(self):
        conditions = [
            {"type": "monitoring_duration", "value": 24, "applied_at": _hours_ago(25)},
            {"type": "risk_target", "value": 0.3, "applied_at": _hours_ago(25)},
        ]
        metrics = _make_metrics(risk_score=0.5)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        # Both should be expired (monitoring_duration expired, propagated to risk_target)
        assert result["action"] == "proceed"
        assert conditions[1].get("duration_hours") == 24

    def test_explicitly_expired_condition_is_skipped(self):
        conditions = [
            {"type": "risk_target", "value": 0.3, "applied_at": _now_iso(), "expired": True}
        ]
        metrics = _make_metrics(risk_score=0.5)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"


# ── complexity_limit tests ───────────────────────────────────────────────

class TestComplexityLimit:
    def test_caps_complexity(self):
        conditions = [{"type": "complexity_limit", "value": 0.3, "applied_at": _now_iso()}]

        capped, warning = enforce_complexity_limit(conditions, 0.8)

        assert capped == 0.3
        assert warning is not None
        assert "0.80" in warning
        assert "0.30" in warning

    def test_no_cap_when_below(self):
        conditions = [{"type": "complexity_limit", "value": 0.5, "applied_at": _now_iso()}]

        capped, warning = enforce_complexity_limit(conditions, 0.3)

        assert capped == 0.3
        assert warning is None

    def test_uses_minimum_of_multiple_caps(self):
        conditions = [
            {"type": "complexity_limit", "value": 0.5, "applied_at": _now_iso()},
            {"type": "complexity_limit", "value": 0.3, "applied_at": _now_iso()},
        ]

        capped, warning = enforce_complexity_limit(conditions, 0.8)

        assert capped == 0.3

    def test_expired_cap_is_skipped(self):
        conditions = [
            {
                "type": "complexity_limit",
                "value": 0.3,
                "applied_at": _hours_ago(25),
                "duration_hours": 24,
            }
        ]

        capped, warning = enforce_complexity_limit(conditions, 0.8)

        assert capped == 0.8
        assert warning is None

    def test_complexity_adjustment_reduce(self):
        conditions = [
            {"type": "complexity_adjustment", "action": "reduce", "target_value": 0.4, "applied_at": _now_iso()}
        ]

        capped, warning = enforce_complexity_limit(conditions, 0.8)

        assert capped == 0.4

    def test_empty_conditions_passthrough(self):
        capped, warning = enforce_complexity_limit([], 0.7)

        assert capped == 0.7
        assert warning is None

    def test_none_conditions_passthrough(self):
        capped, warning = enforce_complexity_limit(None, 0.7)

        assert capped == 0.7
        assert warning is None


# ── Combined conditions ──────────────────────────────────────────────────

class TestCombinedConditions:
    def test_multiple_conditions_all_enforced(self):
        conditions = [
            {"type": "risk_target", "value": 0.3, "applied_at": _now_iso()},
            {"type": "coherence_target", "value": 0.6, "applied_at": _now_iso()},
        ]
        metrics = _make_metrics(risk_score=0.5, coherence=0.4)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "pause"  # risk target alone owns this escalation
        assert len(warnings) == 2

    def test_no_conditions_passthrough(self):
        metrics = _make_metrics()
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions([], metrics, decision)

        assert result is decision
        assert len(warnings) == 0

    def test_non_dict_conditions_skipped(self):
        conditions = ["not a dict", 42, None]
        metrics = _make_metrics(risk_score=0.9)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert len(warnings) == 0

    def test_invalid_value_skipped(self):
        conditions = [{"type": "risk_target", "value": "not a number", "applied_at": _now_iso()}]
        metrics = _make_metrics(risk_score=0.9)
        decision = _make_decision("proceed")

        result, warnings = enforce_post_ode_conditions(conditions, metrics, decision)

        assert result["action"] == "proceed"
        assert len(warnings) == 0


# ── _is_expired edge cases ──────────────────────────────────────────────

class TestIsExpired:
    def test_no_applied_at_not_expired(self):
        assert _is_expired({"type": "risk_target", "value": 0.3}) is False

    def test_no_duration_not_expired(self):
        assert _is_expired({"type": "risk_target", "value": 0.3, "applied_at": _hours_ago(100)}) is False

    def test_bad_timestamp_not_expired(self):
        assert _is_expired({"applied_at": "not-a-date", "duration_hours": 1}) is False
