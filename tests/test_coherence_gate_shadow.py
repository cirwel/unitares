"""Tests for the behavioral-V deviation gate shadow.

This is measurement-only code on the mandatory check-in path, so the properties
that matter most are: it never fires before a baseline is mature, it never
mutates a decision, and an ineligible agent is an explicit observation rather
than an absence that later reads as agreement.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coherence_gate_shadow import (
    K_BLOCK,
    K_FLOOR,
    K_PAUSE,
    RECENT_MIN_SAMPLES,
    STATISTIC_VERSION,
    coherence_gate_shadow_enabled,
    evaluate,
    record,
)


def _beh(z: float, baselined: bool = True):
    """Minimal state whose recent, leave-current-out V score is exactly z.

    A constant prior forces the calibrated 0.05 scale floor, so current=z*.05.
    All test z values keep V inside its valid [-1, 1] range.
    """
    prior = [0.0] * RECENT_MIN_SAMPLES
    current = z * 0.05
    return SimpleNamespace(
        is_baselined=baselined,
        V=current,
        V_history=prior + [current],
        alphas={"V": 0.10},
    )


# ── maturity gate ──────────────────────────────────────────────────────────


def test_immature_baseline_fires_nothing():
    # An agent whose dispersion cannot yet be estimated must not be gated
    # against it, however extreme the raw number looks.
    out = evaluate(_beh(-9.0, baselined=False), fleet_action="approve")
    assert out["eligible"] is False
    assert out["would_action"] is None
    assert out["v_zscore"] is None
    assert out["eligibility_reason"] == "behavioral_baseline_immature"


def test_recent_history_is_a_separate_maturity_gate():
    state = _beh(-9.0)
    state.V_history = state.V_history[-10:]
    out = evaluate(state, fleet_action="approve")
    assert out["eligible"] is False
    assert out["eligibility_reason"] == "insufficient_recent_history"
    assert out["would_action"] is None


def test_ineligible_agrees_is_none_not_true():
    # agrees=True on an ineligible row would inflate any later agreement rate
    # with agents the gate never actually judged.
    out = evaluate(_beh(0.0, baselined=False), fleet_action="approve")
    assert out["agrees"] is None


# ── tier boundaries ────────────────────────────────────────────────────────


@pytest.mark.parametrize("z,expected", [
    (0.0, "proceed"),
    (-2.9, "proceed"),
    (-K_PAUSE, "coherence_pause"),
    (-3.5, "coherence_pause"),
    (-K_BLOCK, "hard_block"),
    (-4.5, "hard_block"),
    (-K_FLOOR, "hard_block_floor"),
    (-9.0, "hard_block_floor"),
    (K_PAUSE, "coherence_pause"),
    (K_BLOCK, "hard_block"),
    (K_FLOOR, "hard_block_floor"),
])
def test_tier_boundaries(z, expected):
    assert evaluate(_beh(z), fleet_action="approve")["would_action"] == expected


def test_positive_and_negative_deviations_have_equal_severity():
    # V sign describes direction (hot vs careful), not health. Equal-magnitude
    # movement in either direction therefore gets the same shadow tier.
    for z in (1.0, 3.0, 4.0, 5.0):
        assert evaluate(_beh(z), "approve")["would_action"] == evaluate(
            _beh(-z), "approve"
        )["would_action"]


def test_current_value_is_excluded_from_its_own_baseline():
    out = evaluate(_beh(-K_FLOOR), fleet_action="approve")
    assert out["v_zscore"] == -K_FLOOR
    assert out["sample_mean"] == 0.0
    assert out["sample_std"] == 0.0
    assert out["scale_source"] == "floor"
    assert out["effective_scale"] == 0.05


def test_payload_versions_the_corrected_two_sided_statistic():
    out = evaluate(_beh(3.5), fleet_action="approve")
    assert out["statistic_version"] == STATISTIC_VERSION
    assert out["tail"] == "two_sided"
    assert out["v_standardized_residual"] == 3.5
    assert out["v_deviation_magnitude"] == 3.5
    assert out["deviation_direction"] == "higher_v"


# ── agreement accounting ───────────────────────────────────────────────────


def test_agreement_when_both_proceed():
    out = evaluate(_beh(-0.5), fleet_action="approve")
    assert out["agrees"] is True


def test_divergence_when_proprioceptive_fires_and_fleet_does_not():
    # The case the shadow exists to count: the fleet constant sees nothing
    # because the agent's absolute coherence is unremarkable, while the agent
    # is far outside its OWN normal.
    out = evaluate(_beh(-6.0), fleet_action="approve")
    assert out["would_action"] == "hard_block_floor"
    assert out["agrees"] is False


def test_agreement_when_both_pause():
    out = evaluate(_beh(-4.0), fleet_action="coherence_pause")
    assert out["agrees"] is True


# ── contract / safety ──────────────────────────────────────────────────────


def test_shape_is_stable_across_eligibility():
    # A consumer should never have to branch on which keys exist.
    assert set(evaluate(_beh(-1.0), "approve")) == set(
        evaluate(_beh(-1.0, baselined=False), "approve")
    )


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("UNITARES_COHERENCE_GATE_SHADOW", raising=False)
    assert coherence_gate_shadow_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "on", "YES"])
def test_flag_accepts_truthy(monkeypatch, val):
    monkeypatch.setenv("UNITARES_COHERENCE_GATE_SHADOW", val)
    assert coherence_gate_shadow_enabled() is True


def test_record_never_raises_into_checkin():
    # Optional measurement must not be able to cost an agent a check-in.
    sink = MagicMock()
    sink.log_coherence_gate_shadow.side_effect = RuntimeError("audit down")
    record(sink, "agent-1", evaluate(_beh(-4.0), "approve"))  # must not raise


def test_record_passes_payload_through():
    sink = MagicMock()
    payload = evaluate(_beh(-4.0), "approve")
    record(sink, "agent-1", payload)
    kw = sink.log_coherence_gate_shadow.call_args.kwargs
    assert kw["agent_id"] == "agent-1"
    assert kw["would_action"] == "hard_block"
    assert kw["fleet_action"] == "approve"
    assert kw["statistic_version"] == STATISTIC_VERSION
    assert kw["tail"] == "two_sided"


def test_payload_matches_real_audit_logger_contract(tmp_path, monkeypatch):
    """The expanding shadow payload must stay accepted by the real sink."""
    from src.audit_log import AuditLogger

    monkeypatch.setenv("UNITARES_AUDIT_WRITE_JSONL", "1")
    monkeypatch.setattr(AuditLogger, "_event_loop", None)
    path = tmp_path / "audit.jsonl"
    record(AuditLogger(path), "agent-1", evaluate(_beh(3.5), "approve"))

    row = json.loads(path.read_text().strip())
    assert row["event_type"] == "coherence_gate_shadow"
    assert row["details"]["statistic_version"] == STATISTIC_VERSION
    assert row["details"]["v_standardized_residual"] == 3.5
    assert row["details"]["scale_source"] == "floor"
