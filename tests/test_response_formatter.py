"""
Tests for src/mcp_handlers/response_formatter.py — Response mode filtering.

Tests _format_minimal, _format_compact, _strip_context (pure dict operations),
and format_response routing (mocked for standard mode which needs GovernanceState).
"""

import pytest
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.response_formatter import (
    format_response,
    _format_minimal,
    _format_compact,
    _format_mirror,
    _strip_context,
)


# ============================================================================
# Sample response data for testing
# ============================================================================

def _sample_response():
    return {
        "agent_id": "test-agent-123",
        "status": "approved",
        "health_status": "healthy",
        "health_message": "All systems nominal",
        "decision": {
            "action": "continue",
            "reason": "Low risk, high coherence",
            "require_human": False,
            "margin": 0.15,
            "nearest_edge": "risk_threshold",
        },
        "metrics": {
            "E": 0.7,
            "I": 0.85,
            "S": 0.1,
            "V": -0.02,
            "coherence": 0.92,
            "risk_score": 0.08,           # smoothed (gating)
            "latest_risk_score": 0.42,    # raw last observation (spike)
            "phi": 1.23,
            "verdict": "approve",
            "lambda1": 0.9,
            "health_status": "healthy",
            "health_message": "All good",
        },
        "trajectory_identity": {
            "trust_tier": {"name": "established"}
        },
        "history": {"decision_history": []},
        # Context fields that may be stripped
        "eisv_labels": {"E": "energy"},
        "learning_context": {"key": "value"},
        "relevant_discoveries": [{"id": "d1"}],
        "onboarding": {"step": 1},
        "welcome": "Hello!",
        "api_key_hint": "sk-***",
        "_onboarding": True,
        # Enrichment bloat (stripped for established agents)
        "convergence_guidance": {"lines": 20},
        "calibration_feedback": {"nested": "dict"},
        "drift_forecast": {"heavy": True},
        "saturation_diagnostics": {"medium": True},
        "perturbation": {"medium": True},
        "actionable_feedback": {"medium": True},
        "state": {"interpretation": "duplicate"},
        "cirs_void_alert": {"internal": True},
        "cirs_state_announce": {"internal": True},
        "outcome_event": {"internal": True},
        "temporal_context": {"low_value": True},
        "identity_reminder": "first 3 only",
        "unitares_v41": {"passthrough": True},
        "pending_dialectic": {"conditional": True},
        "llm_coaching": {"heavy": True},
        "recovery_coaching": {"heavy": True},
        # Internal signals (stripped unconditionally by _strip_context)
        "_mirror_signals": [],
        "_mirror_kg_results": [],
        "_mirror_question": None,
        "_mirror_reflection": None,
        "_has_sensor_data": False,
        "_eisv_validation_warning": "warning",
        "advisories": [],
    }


# ============================================================================
# _format_minimal
# ============================================================================

class TestFormatMinimal:

    def test_basic_fields(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["action"] == "continue"
        assert result["_mode"] == "minimal"
        assert result["E"] == 0.7
        assert result["I"] == 0.85
        assert result["S"] == 0.1
        assert result["V"] == -0.02
        assert result["coherence"] == 0.92

    def test_includes_phi(self):
        """phi is the primary basin discriminator — minimal carried every
        EISV channel except it (compact already includes it; parity)."""
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["phi"] == 1.23

    def test_includes_margin(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["margin"] == 0.15

    def test_includes_nearest_edge(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["nearest_edge"] == "risk_threshold"

    def test_no_margin_when_absent(self):
        data = _sample_response()
        data["decision"]["margin"] = None
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert "margin" not in result

    def test_tip_when_default_mode(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=True, saved_trust_tier=None)
        assert "_tip" in result

    def test_no_tip_when_explicit_mode(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert "_tip" not in result

    def test_trust_tier_included(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier="established")
        assert result["trust_tier"] == "established"

    def test_no_trust_tier_when_none(self):
        data = _sample_response()
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert "trust_tier" not in result

    def test_risk_score_is_canonical_gating_value(self):
        """metrics.risk_score in the response must be the smoothed gating
        value (the one make_decision reasoned over), not the raw spike."""
        data = _sample_response()
        # Sample has risk_score=0.08 (smoothed), latest_risk_score=0.42 (spike).
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["risk_score"] == 0.08
        assert result["risk_score_latest"] == 0.42

    def test_risk_score_latest_missing(self):
        """If latest_risk_score is absent, risk_score still surfaces the
        canonical gating value; latest is None."""
        data = _sample_response()
        data["metrics"].pop("latest_risk_score")
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["risk_score"] == 0.08
        assert result["risk_score_latest"] is None

    def test_empty_decision(self):
        data = _sample_response()
        data["decision"] = {}
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["action"] == "continue"  # default

    def test_non_dict_decision(self):
        data = _sample_response()
        data["decision"] = "not_a_dict"
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["action"] == "continue"

    def test_non_dict_metrics(self):
        data = _sample_response()
        data["metrics"] = "not_a_dict"
        result = _format_minimal(data, using_default_mode=False, saved_trust_tier=None)
        assert result["E"] is None
        assert result["I"] is None


# ============================================================================
# _format_compact
# ============================================================================

class TestFormatCompact:

    def test_basic_structure(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert result["success"] is True
        assert result["_mode"] == "compact"
        assert result["agent_id"] == "test-agent-123"
        assert "summary" in result

    def test_metrics_included(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        m = result["metrics"]
        assert m["E"] == 0.7
        assert m["coherence"] == 0.92
        assert m["phi"] == 1.23

    def test_decision_included(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        d = result["decision"]
        assert d["action"] == "continue"
        assert d["reason"] == "Low risk, high coherence"
        assert d["margin"] == 0.15

    def test_summary_format(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert "continue" in result["summary"]
        assert "healthy" in result["summary"]
        assert "0.92" in result["summary"]

    def test_trust_tier_included(self):
        # #428: compact mode wraps trust_tier with meaning + criteria inline.
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier="established")
        tt = result["trust_tier"]
        assert isinstance(tt, dict)
        assert tt["name"] == "established"
        assert tt["tier"] == 2
        assert "meaning" in tt
        assert "criteria" in tt

    def test_trust_tier_from_dict(self):
        # #428: also accepts the upstream {tier, name, reason} dict shape.
        data = _sample_response()
        result = _format_compact(
            data,
            using_default_mode=False,
            saved_trust_tier={"tier": 2, "name": "established", "reason": "test"},
        )
        tt = result["trust_tier"]
        assert tt["tier"] == 2
        assert tt["name"] == "established"
        assert tt["reason"] == "test"
        assert "meaning" in tt
        assert "criteria" in tt

    def test_tip_when_default_mode(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=True, saved_trust_tier=None)
        assert "_tip" in result

    def test_no_tip_when_explicit(self):
        data = _sample_response()
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert "_tip" not in result

    def test_risk_score_is_canonical_gating_value(self):
        """Compact response's metrics.risk_score must be the smoothed gating
        value (matches decision.reason). Raw last observation lives in
        risk_score_latest."""
        data = _sample_response()
        # Sample has risk_score=0.08 (smoothed), latest_risk_score=0.42 (spike).
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert result["metrics"]["risk_score"] == 0.08
        assert result["metrics"]["risk_score_latest"] == 0.42

    def test_risk_score_latest_missing(self):
        """If latest_risk_score is absent, risk_score still surfaces canonical
        gating value; risk_score_latest is None."""
        data = _sample_response()
        data["metrics"]["latest_risk_score"] = None
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert result["metrics"]["risk_score"] == 0.08
        assert result["metrics"]["risk_score_latest"] is None

    def test_empty_metrics(self):
        data = _sample_response()
        data["metrics"] = {}
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert result["metrics"]["E"] is None

    def test_non_dict_metrics(self):
        data = _sample_response()
        data["metrics"] = "nope"
        result = _format_compact(data, using_default_mode=False, saved_trust_tier=None)
        assert result["metrics"]["E"] is None


# ============================================================================
# _strip_context
# ============================================================================

class TestStripContext:

    def test_strips_eisv_labels(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=False)
        assert "eisv_labels" not in data

    def test_strips_learning_context_for_established(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=False)
        assert "learning_context" not in data
        assert "relevant_discoveries" not in data
        assert "onboarding" not in data
        assert "welcome" not in data

    def test_strips_enrichment_bloat_for_established(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=False)
        for key in [
            "convergence_guidance", "calibration_feedback", "trajectory_identity",
            "drift_forecast", "saturation_diagnostics", "perturbation",
            "actionable_feedback", "state", "cirs_void_alert",
            "cirs_state_announce", "outcome_event", "temporal_context",
            "identity_reminder", "unitares_v41", "pending_dialectic",
            "llm_coaching", "recovery_coaching",
        ]:
            assert key not in data, f"{key} should be stripped for established agents"

    def test_preserves_enrichment_for_new_agent(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=True, key_was_generated=False, api_key_auto_retrieved=False)
        assert "learning_context" in data
        assert "onboarding" in data
        assert "convergence_guidance" in data
        assert "calibration_feedback" in data

    def test_strips_internal_signals_unconditionally(self):
        data = _sample_response()
        # Set non-empty values to verify they get stripped
        data["_mirror_signals"] = ["signal"]
        data["_mirror_kg_results"] = [{"summary": "result"}]
        data["_mirror_question"] = "question"
        data["_mirror_reflection"] = "reflect"
        _strip_context(data, is_new_agent=True, key_was_generated=False, api_key_auto_retrieved=False)
        assert "_mirror_signals" not in data
        assert "_mirror_kg_results" not in data
        assert "_mirror_question" not in data
        assert "_mirror_reflection" not in data
        assert "_has_sensor_data" not in data
        assert "_eisv_validation_warning" not in data

    def test_strips_empty_advisories(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=True, key_was_generated=False, api_key_auto_retrieved=False)
        assert "advisories" not in data

    def test_preserves_nonempty_advisories(self):
        data = _sample_response()
        data["advisories"] = [{"msg": "important"}]
        _strip_context(data, is_new_agent=True, key_was_generated=False, api_key_auto_retrieved=False)
        assert "advisories" in data

    def test_strips_api_key_hint_for_established(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=False)
        assert "api_key_hint" not in data
        assert "_onboarding" not in data

    def test_preserves_api_key_hint_when_generated(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=True, api_key_auto_retrieved=False)
        assert "api_key_hint" in data

    def test_preserves_api_key_hint_when_auto_retrieved(self):
        data = _sample_response()
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=True)
        assert "api_key_hint" in data

    def test_modifies_in_place(self):
        data = {"eisv_labels": True}
        _strip_context(data, is_new_agent=True, key_was_generated=False, api_key_auto_retrieved=False)
        assert "eisv_labels" not in data

    def test_handles_missing_keys_gracefully(self):
        data = {}
        _strip_context(data, is_new_agent=False, key_was_generated=False, api_key_auto_retrieved=False)
        # Should not raise


# ============================================================================
# format_response routing
# ============================================================================

class TestFormatResponse:

    def test_full_mode_returns_as_is(self):
        data = _sample_response()
        original_keys = set(data.keys())
        result = format_response(data, {"response_mode": "full"})
        assert set(result.keys()) == original_keys

    def test_minimal_mode(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "minimal"})
        assert result["_mode"] == "minimal"
        assert result["action"] == "continue"

    def test_compact_mode(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "compact"})
        assert result["_mode"] == "compact"
        assert "summary" in result

    def test_lite_alias_for_compact(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "lite"})
        assert result["_mode"] == "compact"

    def test_auto_mode_healthy_becomes_mirror_for_disembodied(self):
        data = _sample_response()
        data["health_status"] = "healthy"
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "mirror"  # Disembodied (no sensor_data) -> mirror

    def test_auto_mode_healthy_becomes_minimal_for_embodied(self):
        data = _sample_response()
        data["health_status"] = "healthy"
        data["_has_sensor_data"] = True
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "minimal"  # Embodied (has sensor_data) -> minimal

    def test_auto_mode_at_risk_becomes_standard(self):
        """auto mode with at_risk health should become standard (needs GovernanceState)."""
        data = _sample_response()
        data["health_status"] = "at_risk"
        # Standard mode needs GovernanceState imports — mock them
        mock_state = MagicMock()
        mock_state.interpret_state.return_value = {"summary": "At risk"}
        with patch("src.mcp_handlers.response_formatter.GovernanceState", return_value=mock_state, create=True):
            with patch("src.mcp_handlers.response_formatter._format_standard") as mock_std:
                mock_std.return_value = {"_mode": "standard", "state": "at risk"}
                result = format_response(data, {"response_mode": "auto"})
                mock_std.assert_called_once()

    def test_auto_mode_unknown_becomes_compact(self):
        data = _sample_response()
        data["health_status"] = "unknown"
        data["metrics"]["health_status"] = "unknown"
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "compact"

    def test_env_var_override(self):
        data = _sample_response()
        with patch.dict(os.environ, {"UNITARES_PROCESS_UPDATE_RESPONSE_MODE": "compact"}):
            result = format_response(data, {})  # No per-call mode
            assert result["_mode"] == "compact"

    def test_trust_tier_propagates_through_format_response_compact(self):
        # #428: end-to-end — trajectory_identity.trust_tier dict at the top
        # carries through to the compact-mode trust_tier dict.
        data = _sample_response()
        data["trajectory_identity"]["trust_tier"] = {"tier": 2, "name": "established"}
        result = format_response(data, {"response_mode": "compact"})
        tt = result["trust_tier"]
        assert isinstance(tt, dict)
        assert tt["name"] == "established"
        assert "meaning" in tt
        assert "criteria" in tt

    def test_trust_tier_propagates_through_format_response_mirror(self):
        # Same end-to-end check for mirror mode.
        data = _sample_response()
        data["trajectory_identity"]["trust_tier"] = {"tier": 3, "name": "verified"}
        # Force mirror via no sensor_data + healthy.
        data["health_status"] = "healthy"
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "mirror"
        tt = result["trust_tier"]
        assert tt["name"] == "verified"
        assert tt["tier"] == 3
        assert "meaning" in tt

    def test_per_call_overrides_env_var(self):
        data = _sample_response()
        with patch.dict(os.environ, {"UNITARES_PROCESS_UPDATE_RESPONSE_MODE": "compact"}):
            result = format_response(data, {"response_mode": "minimal"})
            assert result["_mode"] == "minimal"

    def test_agent_preference_override(self):
        data = _sample_response()
        meta = MagicMock()
        meta.preferences = {"verbosity": "compact"}
        result = format_response(data, {}, meta=meta)
        assert result["_mode"] == "compact"

    def test_per_call_overrides_agent_pref(self):
        data = _sample_response()
        meta = MagicMock()
        meta.preferences = {"verbosity": "compact"}
        result = format_response(data, {"response_mode": "minimal"}, meta=meta)
        assert result["_mode"] == "minimal"

    def test_strip_context_applied_for_minimal(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "minimal"}, is_new_agent=False)
        # eisv_labels stripped
        assert "eisv_labels" not in result

    def test_strip_context_applied_for_compact(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "compact"}, is_new_agent=False)
        assert "eisv_labels" not in result

    def test_trust_tier_preserved(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "minimal"})
        assert result.get("trust_tier") == "established"

    def test_trust_tier_none_when_no_trajectory(self):
        data = _sample_response()
        data.pop("trajectory_identity")
        result = format_response(data, {"response_mode": "minimal"})
        assert "trust_tier" not in result

    def test_meta_without_preferences(self):
        data = _sample_response()
        meta = MagicMock()
        meta.preferences = None
        result = format_response(data, {"response_mode": "minimal"}, meta=meta)
        assert result["_mode"] == "minimal"

    def test_meta_without_preferences_attr(self):
        data = _sample_response()
        meta = object()  # No preferences attribute
        result = format_response(data, {"response_mode": "minimal"}, meta=meta)
        assert result["_mode"] == "minimal"


# ============================================================================
# _format_mirror
# ============================================================================

class TestFormatMirror:

    def test_basic_output_shape(self):
        data = _sample_response()
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["_mode"] == "mirror"
        assert result["success"] is True
        assert "verdict" in result
        assert "mirror" in result
        assert isinstance(result["mirror"], list)

    def test_verdict_from_decision(self):
        data = _sample_response()
        data["decision"]["action"] = "pause"
        result = _format_mirror(data, saved_trust_tier=None)
        # #428: verdict is now wrapped with meaning + next_action at the
        # response surface. The raw value lives at .value.
        assert result["verdict"]["value"] == "pause"
        assert "Needs attention" in result["verdict"]["meaning"]
        assert "next_action" in result["verdict"]

    def test_calibration_insight_inverted(self):
        data = _sample_response()
        data["learning_context"] = {
            "calibration": {
                "insight": "INVERTED CALIBRATION: High confidence correlates with LOWER trajectory health.",
                "total_decisions": 15,
                "trajectory_health": 0.65,
                "overall_accuracy": 0.65,  # legacy alias from strategic bins
            }
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert any("inverted" in s.lower() for s in result["mirror"])
        # Must be labeled as a fleet-wide trend, not the caller's personal
        # confidence — the underlying data is from a module-level singleton
        # aggregated across all agents. Previously the string was "Your
        # confidence tends to be inverted ..." which misled fresh agents
        # into thinking they had accumulated history.
        assert any("fleet" in s.lower() for s in result["mirror"]), \
            "INVERTED calibration signal must be labeled fleet-wide"
        assert not any("accuracy" in s.lower() for s in result["mirror"]), \
            "Mirror must not call strategic trajectory-health bins accuracy"
        assert any("trajectory health" in s.lower() for s in result["mirror"])

    def test_calibration_insight_normal_labels_strategic_bins_as_trajectory_health(self):
        data = _sample_response()
        data["learning_context"] = {
            "calibration": {
                "insight": "Well calibrated",
                "total_decisions": 20,
                "trajectory_health": 0.82,
                "overall_accuracy": 0.82,  # legacy alias from strategic bins
                "high_confidence_trajectory_health": 0.91,
                "low_confidence_trajectory_health": 0.74,
            }
        }
        result = _format_mirror(data, saved_trust_tier=None)
        signal = next(s for s in result["mirror"] if "fleet calibration" in s.lower())
        assert "82%" in signal
        assert "trajectory health" in signal.lower()
        assert "high-conf health" in signal.lower()
        assert "low-conf health" in signal.lower()
        assert "accuracy" not in signal.lower()
        # Same scope concern as the inverted case — the 20 decisions are
        # fleet-wide, not per-agent. Label must match the dashboard, which
        # renders the same singleton under a "Fleet-wide" header.
        assert "fleet" in signal.lower(), \
            "Calibration trajectory-health signal must be labeled fleet-wide"

    def test_fleet_calibration_suppressed_when_healthy(self):
        # At steady-high fleet health the line is a constant dashboard stat with
        # no per-turn signal — it must NOT appear in a per-agent mirror.
        data = _sample_response()
        data["learning_context"] = {
            "calibration": {
                "insight": "Well calibrated",
                "total_decisions": 41705,
                "trajectory_health": 0.99,
                "high_confidence_trajectory_health": 0.99,
                "low_confidence_trajectory_health": 0.99,
            }
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert not any("fleet calibration" in s.lower() for s in result["mirror"]), \
            "Healthy fleet calibration must be suppressed (no non-sequitur dashboard stat)"

    def test_complexity_divergence_signal_is_neutral_not_interrogation(self):
        data = _sample_response()
        data["calibration_feedback"] = {
            "complexity": {
                "reported": 0.8,
                "derived": 0.28,
                "discrepancy": 0.52,
            }
        }
        result = _format_mirror(data, saved_trust_tier=None)
        # Recorded observation, not a demand to justify "difficulty".
        assert any("you reported 0.80" in s and "surface estimate" in s for s in result["mirror"])
        assert not any(
            "what's driving" in s.lower() or "sense of difficulty" in s.lower()
            for s in result["mirror"]
        )
        # The estimate's basis is disclosed inline — it reads output
        # surface, not task content (dogfood 2026-06-10).
        assert any("not task content" in s for s in result["mirror"])

    def test_complexity_line_fires_when_divergence_novel(self):
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.3,
            "complexity_divergence": 0.4,
            "divergence_novel": True,
        }
        result = _format_mirror(data, saved_trust_tier=None)
        line = next(s for s in result["mirror"] if "Complexity calibration" in s)
        assert "you reported 0.70" in line
        assert "0.30" in line
        assert "not task content" in line

    def test_complexity_line_suppressed_when_divergence_not_novel(self):
        """A stable session-long gap must not repeat the same line on
        every check-in — that's the noise this gate removes."""
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.3,
            "complexity_divergence": 0.4,
            "divergence_novel": False,
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert not any("Complexity calibration" in s for s in result["mirror"])

    def test_complexity_line_back_compat_without_novelty_key(self):
        """Payloads built without divergence_novel (older builders,
        hand-built dicts) keep the raw-threshold behavior."""
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.3,
            "complexity_divergence": 0.4,
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert any("Complexity calibration" in s for s in result["mirror"])

    def test_proprioceptive_numbers_top_level(self):
        """phi/coherence/risk_score surface as data beside margin —
        without them a mirror-mode agent needs a second tool call to
        learn its own state."""
        data = _sample_response()
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["phi"] == 1.23
        assert result["coherence"] == 0.92
        assert result["risk_score"] == 0.08
        # Data keys, not prose — the signals list must not gain a
        # numbers line out of this.
        assert not any("phi" in s.lower() for s in result["mirror"])

    def test_proprioceptive_numbers_omitted_when_absent(self):
        data = _sample_response()
        for key in ("phi", "coherence", "risk_score"):
            data["metrics"].pop(key, None)
        result = _format_mirror(data, saved_trust_tier=None)
        assert "phi" not in result
        assert "coherence" not in result
        assert "risk_score" not in result

    def test_mirror_signals_from_enrichment(self):
        data = _sample_response()
        data["_mirror_signals"] = ["Your reports show low variance"]
        result = _format_mirror(data, saved_trust_tier=None)
        assert "Your reports show low variance" in result["mirror"]

    def test_kg_results_surfaced(self):
        data = _sample_response()
        data["_mirror_kg_results"] = [
            {"summary": "Coherence issue found", "agent_id": "AlvaNoto", "relevance": 0.42}
        ]
        result = _format_mirror(data, saved_trust_tier=None)
        assert "relevant_prior_work" in result
        assert result["relevant_prior_work"][0]["by"] == "AlvaNoto"

    def test_legacy_mirror_question_surfaces_as_reflection(self):
        # Back-compat: an older enrichment that still sets _mirror_question is
        # surfaced under the descriptive `reflection` key (no `question` key).
        data = _sample_response()
        data["_mirror_question"] = "Something an old enrichment set"
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["reflection"] == "Something an old enrichment set"
        assert "question" not in result

    def test_trust_tier_wrapped_with_meaning(self):
        # #428: mirror mode wraps trust_tier with meaning + criteria inline.
        data = _sample_response()
        result = _format_mirror(data, saved_trust_tier="established")
        tt = result["trust_tier"]
        assert isinstance(tt, dict)
        assert tt["name"] == "established"
        assert tt["tier"] == 2
        assert "meaning" in tt
        assert "criteria" in tt

    def test_trust_tier_from_dict_preserves_upstream_fields(self):
        # When upstream passes the full {tier, name, reason} dict, mirror
        # preserves all fields and layers meaning + criteria on top.
        data = _sample_response()
        result = _format_mirror(
            data,
            saved_trust_tier={"tier": 1, "name": "emerging", "reason": "test reason"},
        )
        tt = result["trust_tier"]
        assert tt["name"] == "emerging"
        assert tt["tier"] == 1
        assert tt["reason"] == "test reason"
        assert "meaning" in tt

    def test_no_trust_tier_when_none(self):
        data = _sample_response()
        result = _format_mirror(data, saved_trust_tier=None)
        assert "trust_tier" not in result

    def test_reflection_surfaced_under_reflection_key(self):
        data = _sample_response()
        data["_mirror_reflection"] = "You're close to a coherence edge."
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["reflection"] == "You're close to a coherence edge."
        assert "question" not in result

    def test_trust_tier_included(self):
        # #428: mirror now wraps with glossary; the bare name resolves
        # through reverse-lookup so existing string callers compose.
        data = _sample_response()
        result = _format_mirror(data, saved_trust_tier="established")
        assert isinstance(result["trust_tier"], dict)
        assert result["trust_tier"]["name"] == "established"

    def test_thread_context_preserved(self):
        data = _sample_response()
        data["thread_context"] = {"thread_id": "t123", "position": 2}
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["thread_context"]["thread_id"] == "t123"

    def test_no_signals_gives_steady_state(self):
        data = _sample_response()
        # No enrichment data, no calibration, no divergence, no capping, no restorative
        data.pop("learning_context", None)
        data.pop("calibration_feedback", None)
        data.pop("confidence_reliability", None)
        data.pop("continuity", None)
        data.pop("restorative", None)
        data.pop("relevant_discoveries", None)
        data.pop("_mirror_question", None)
        data.pop("_mirror_reflection", None)
        result = _format_mirror(data, saved_trust_tier=None)
        assert "steady state" in result["mirror"][0].lower()
        assert "reflection" not in result
        assert "question" not in result

    def test_margin_included_when_tight(self):
        data = _sample_response()
        data["decision"]["margin"] = 0.05
        result = _format_mirror(data, saved_trust_tier=None)
        assert result["margin"] == 0.05

    def test_margin_excluded_when_comfortable(self):
        data = _sample_response()
        data["decision"]["margin"] = 0.2
        result = _format_mirror(data, saved_trust_tier=None)
        assert "margin" not in result

    def test_identity_notifications_surfaced(self):
        data = _sample_response()
        data["_identity_notifications"] = [{"message": "Identity accessed from new session"}]
        result = _format_mirror(data, saved_trust_tier=None)
        assert "identity_notifications" in result

    def test_observed_confidence_surfaced(self):
        data = _sample_response()
        data["confidence_reliability"] = {
            "source": "observed",
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert any("derived" in s.lower() for s in result["mirror"])

    def test_continuity_divergence_surfaced(self):
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.22,
            "complexity_divergence": 0.48,
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert any("you reported 0.70" in s for s in result["mirror"])
        assert any("0.22" in s for s in result["mirror"])

    def test_continuity_takes_precedence_over_calibration_feedback(self):
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.22,
            "complexity_divergence": 0.48,
        }
        data["calibration_feedback"] = {
            "complexity": {"reported": 0.8, "derived": 0.28, "discrepancy": 0.52}
        }
        result = _format_mirror(data, saved_trust_tier=None)
        # Should use continuity (0.7/0.22), not calibration_feedback (0.8/0.28)
        assert any("you reported 0.70" in s for s in result["mirror"])

    def test_pace_surfaced_descriptively(self):
        data = _sample_response()
        data["restorative"] = {
            "needs_restoration": True,
            "reason": "complexity divergence (0.48 cumulative, logged for calibration)",
        }
        result = _format_mirror(data, saved_trust_tier=None)
        # Reflected as a descriptive "Pace:" line — NOT "Restorative action:"
        # (which named an action = the verdict's voice, not the mirror's).
        pace_lines = [s for s in result["mirror"] if s.startswith("Pace:")]
        assert pace_lines, result["mirror"]
        assert "0.48 cumulative" in pace_lines[0]
        assert not any("restorative action" in s.lower() for s in result["mirror"])

    def test_existing_discoveries_merged_into_prior_work(self):
        data = _sample_response()
        data["relevant_discoveries"] = [
            {"summary": "Inverted U curve in calibration", "agent_id": "Alva_Noto", "score": 0.85}
        ]
        result = _format_mirror(data, saved_trust_tier=None)
        assert "relevant_prior_work" in result
        assert result["relevant_prior_work"][0]["summary"] == "Inverted U curve in calibration"

    def test_calibration_feedback_fallback_when_no_continuity(self):
        """calibration_feedback is used when continuity data is absent."""
        data = _sample_response()
        data["calibration_feedback"] = {
            "complexity": {"reported": 0.8, "derived": 0.28, "discrepancy": 0.52}
        }
        result = _format_mirror(data, saved_trust_tier=None)
        assert any("you reported 0.80" in s for s in result["mirror"])

    def test_complexity_divergence_suppressed_early(self):
        """With meta.total_updates <= 3, complexity divergence is suppressed."""
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.22,
            "complexity_divergence": 0.48,
        }
        data["calibration_feedback"] = {
            "complexity": {"reported": 0.8, "derived": 0.28, "discrepancy": 0.52}
        }
        meta = MagicMock()
        meta.total_updates = 1
        result = _format_mirror(data, saved_trust_tier=None, meta=meta)
        # No complexity divergence signals should appear. (Council fold,
        # PR #603: the old assertion checked for "complexity=", a string
        # no signal ever contained — a no-op that passed regardless.)
        assert not any("Complexity calibration" in s for s in result["mirror"]), (
            f"Unexpected complexity signal on early check-in: {result['mirror']}"
        )

    def test_complexity_divergence_shown_after_baseline(self):
        """With meta.total_updates > 3, complexity divergence appears normally."""
        data = _sample_response()
        data["continuity"] = {
            "self_reported_complexity": 0.7,
            "derived_complexity": 0.22,
            "complexity_divergence": 0.48,
        }
        meta = MagicMock()
        meta.total_updates = 10
        result = _format_mirror(data, saved_trust_tier=None, meta=meta)
        assert any("you reported 0.70" in s for s in result["mirror"])


class TestFormatResponseMirror:

    def test_explicit_mirror_mode(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "mirror"})
        assert result["_mode"] == "mirror"

    def test_auto_selects_mirror_for_disembodied(self):
        data = _sample_response()
        data["health_status"] = "healthy"
        data["_has_sensor_data"] = False
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "mirror"

    def test_auto_selects_minimal_for_embodied(self):
        data = _sample_response()
        data["health_status"] = "healthy"
        data["_has_sensor_data"] = True
        result = format_response(data, {"response_mode": "auto"})
        assert result["_mode"] == "minimal"

    def test_strip_context_applied_for_mirror(self):
        data = _sample_response()
        result = format_response(data, {"response_mode": "mirror"}, is_new_agent=False)
        assert "eisv_labels" not in result


# ============================================================================
# Task 2: prediction_id + warnings pass-through (spec §6 + §2)
# ============================================================================

def _make_governance_state_mocks():
    """Create sys.modules stubs for governance_state + governance_core."""
    mock_gs_instance = MagicMock()
    mock_gs_instance.interpret_state.return_value = {"summary": "mocked"}
    mock_gs_module = MagicMock()
    mock_gs_module.GovernanceState = MagicMock(return_value=mock_gs_instance)

    mock_core_module = MagicMock()
    mock_core_module.State = MagicMock()
    mock_core_module.Theta = MagicMock()
    mock_core_module.DEFAULT_THETA = MagicMock()
    return mock_gs_module, mock_core_module


class TestFormatStandardPreservesPredictionId:
    """_format_standard must pass prediction_id and warnings through (spec §6 + §2).

    _format_standard does local imports (from governance_state import ...) so we
    must inject into sys.modules. Same strategy as other tests in this suite that
    call format_response with auto→standard routing (those mock _format_standard
    wholesale; we mock the deps so we can actually exercise the body).
    """

    def _call_format_standard(self, response_data):
        """Call _format_standard with all required module-level deps mocked."""
        import sys
        from src.mcp_handlers.response_formatter import _format_standard
        gs_mock, core_mock = _make_governance_state_mocks()
        with patch.dict(sys.modules, {
            "governance_state": gs_mock,
            "governance_core": core_mock,
        }):
            return _format_standard(response_data, task_type="general")

    def test_prediction_id_passes_through(self):
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "prediction_id": "abc-123",
        }
        result = self._call_format_standard(response_data)
        assert result.get("prediction_id") == "abc-123"

    def test_warnings_passes_through(self):
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "warnings": ["evidence record failed for tool=pytest"],
        }
        result = self._call_format_standard(response_data)
        assert result.get("warnings") == ["evidence record failed for tool=pytest"]

    def test_no_prediction_id_when_absent(self):
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
        }
        result = self._call_format_standard(response_data)
        assert "prediction_id" not in result

    def test_no_warnings_when_absent(self):
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
        }
        result = self._call_format_standard(response_data)
        assert "warnings" not in result


class TestFormatMirrorPreservesPredictionId:
    def test_prediction_id_passes_through(self):
        from src.mcp_handlers.response_formatter import _format_mirror
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "prediction_id": "abc-123",
        }
        result = _format_mirror(response_data, saved_trust_tier=None, meta=None)
        assert result.get("prediction_id") == "abc-123"

    def test_warnings_passes_through(self):
        from src.mcp_handlers.response_formatter import _format_mirror
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "warnings": ["W"],
        }
        result = _format_mirror(response_data, saved_trust_tier=None, meta=None)
        assert result.get("warnings") == ["W"]

    def test_no_prediction_id_when_absent(self):
        from src.mcp_handlers.response_formatter import _format_mirror
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
        }
        result = _format_mirror(response_data, saved_trust_tier=None, meta=None)
        assert "prediction_id" not in result


class TestFormatCompactPreservesPredictionId:
    def test_prediction_id_passes_through(self):
        from src.mcp_handlers.response_formatter import _format_compact
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "prediction_id": "abc-123",
        }
        result = _format_compact(response_data, using_default_mode=False, saved_trust_tier=None)
        assert result.get("prediction_id") == "abc-123"

    def test_warnings_passes_through(self):
        from src.mcp_handlers.response_formatter import _format_compact
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "warnings": ["compact-warning"],
        }
        result = _format_compact(response_data, using_default_mode=False, saved_trust_tier=None)
        assert result.get("warnings") == ["compact-warning"]

    def test_no_prediction_id_when_absent(self):
        from src.mcp_handlers.response_formatter import _format_compact
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
        }
        result = _format_compact(response_data, using_default_mode=False, saved_trust_tier=None)
        assert "prediction_id" not in result


class TestFormatMinimalStripsPredictionIdButPreservesWarnings:
    def test_minimal_does_not_include_prediction_id(self):
        # Spec §6: minimal mode is bandwidth-constrained; prediction_id is a correlation
        # handle with no correctness value, so it is stripped intentionally.
        from src.mcp_handlers.response_formatter import _format_minimal
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "prediction_id": "abc-123",
        }
        result = _format_minimal(response_data, using_default_mode=False, saved_trust_tier=None)
        assert "prediction_id" not in result

    def test_minimal_preserves_warnings(self):
        # Warnings are correctness signals (e.g. "evidence record failed for tool=pytest").
        # Dropping them in minimal mode silently hides pipeline failures from bandwidth-
        # constrained clients — so warnings must survive regardless of verbosity mode.
        from src.mcp_handlers.response_formatter import _format_minimal
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
            "warnings": ["evidence record failed for tool=pytest"],
        }
        result = _format_minimal(response_data, using_default_mode=False, saved_trust_tier=None)
        assert result.get("warnings") == ["evidence record failed for tool=pytest"]

    def test_minimal_no_warnings_key_when_absent(self):
        from src.mcp_handlers.response_formatter import _format_minimal
        response_data = {
            "decision": {"action": "proceed"},
            "metrics": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0, "phi": 0.7},
        }
        result = _format_minimal(response_data, using_default_mode=False, saved_trust_tier=None)
        assert "warnings" not in result


class TestUpdateResponseServiceMergesWarnings:
    def test_ctx_warnings_appear_in_response_data(self):
        # build_process_update_response_data should merge ctx.warnings (de-duped)
        # into response_data["warnings"].
        from src.services.update_response_service import build_process_update_response_data
        result = build_process_update_response_data(
            result={},
            agent_id="test-agent",
            identity_assurance={},
            monitor=None,
            ctx_warnings=["w1", "w1", "w2"],  # duplicate to verify de-dup
        )
        assert sorted(result.get("warnings", [])) == ["w1", "w2"]

    def test_no_warnings_key_when_ctx_warnings_empty(self):
        from src.services.update_response_service import build_process_update_response_data
        result = build_process_update_response_data(
            result={},
            agent_id="test-agent",
            identity_assurance={},
            monitor=None,
            ctx_warnings=[],
        )
        assert "warnings" not in result

    def test_no_warnings_key_when_ctx_warnings_not_provided(self):
        from src.services.update_response_service import build_process_update_response_data
        result = build_process_update_response_data(
            result={},
            agent_id="test-agent",
            identity_assurance={},
            monitor=None,
        )
        assert "warnings" not in result

    def test_warnings_order_preserved_after_dedup(self):
        from src.services.update_response_service import build_process_update_response_data
        result = build_process_update_response_data(
            result={},
            agent_id="test-agent",
            identity_assurance={},
            monitor=None,
            ctx_warnings=["a", "b", "a", "c", "b"],
        )
        # Order of first occurrence preserved; a comes before b before c
        assert result["warnings"] == ["a", "b", "c"]
