"""Novelty gate for the mirror's complexity-calibration line.

`_complexity_divergence_novel` (src/monitor_result.py) decides whether
the divergence is worth surfacing AGAIN: first threshold crossing or a
materially changed signed gap only. Dogfood 2026-06-10: a stable
session-long gap repeated the same mirror line on every check-in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.monitor_result import _complexity_divergence_novel


def _monitor():
    return SimpleNamespace(_last_surfaced_complexity_gap=None)


def _cm(self_cx, derived_cx):
    divergence = abs((self_cx if self_cx is not None else 0.0) - derived_cx)
    return SimpleNamespace(
        self_complexity=self_cx,
        derived_complexity=derived_cx,
        complexity_divergence=divergence,
    )


def test_first_crossing_is_novel():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(0.4)


def test_stable_gap_not_novel_on_repeat():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is False
    # Small wobble within the delta is still the same gap.
    assert _complexity_divergence_novel(m, _cm(0.72, 0.35)) is False


def test_materially_changed_gap_is_novel_again():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    assert _complexity_divergence_novel(m, _cm(0.9, 0.3)) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(0.6)


def test_direction_flip_is_novel():
    """Signed-gap tracking: over→under-reporting of equal magnitude must
    register even though |divergence| is unchanged."""
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.5, 0.3)) is True   # gap +0.2
    assert _complexity_divergence_novel(m, _cm(0.3, 0.5)) is True   # gap −0.2
    assert m._last_surfaced_complexity_gap == pytest.approx(-0.2)


def test_below_threshold_never_novel_and_keeps_state():
    m = _monitor()
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is True
    # Dip below the line threshold: not novel, and the last surfaced gap
    # is retained so returning to the SAME gap does not re-fire.
    assert _complexity_divergence_novel(m, _cm(0.4, 0.3)) is False
    assert m._last_surfaced_complexity_gap == pytest.approx(0.4)
    assert _complexity_divergence_novel(m, _cm(0.7, 0.3)) is False


def test_none_self_complexity_treated_as_zero():
    m = _monitor()
    cm = _cm(None, 0.5)
    assert cm.complexity_divergence == pytest.approx(0.5)
    assert _complexity_divergence_novel(m, cm) is True
    assert m._last_surfaced_complexity_gap == pytest.approx(-0.5)


def test_monitor_without_attribute_uses_getattr_default():
    """Defensive: a monitor object predating the attribute (or a test
    stub) starts from None."""
    bare = SimpleNamespace()
    assert _complexity_divergence_novel(bare, _cm(0.7, 0.3)) is True
    assert bare._last_surfaced_complexity_gap == pytest.approx(0.4)


def test_build_result_exposes_policy_and_unapplied_enforcement_layers():
    """process_update payloads should not collapse EISV measurement,
    policy choice, and actuator state into one opaque decision field."""
    from src.monitor_result import build_result

    monitor = SimpleNamespace(
        agent_id="agent-policy-separation",
        _last_continuity_metrics=None,
        _last_restorative_status=None,
        _last_drift_vector=None,
        _gains_modulated=False,
        adaptive_governor=None,
        state=SimpleNamespace(CE_history=[], resonance_events=0, damping_applied_count=0),
    )
    decision = {
        "action": "pause",
        "sub_action": "coherence_pause",
        "reason": "Coherence needs attention",
        "guidance": "Simplify and regroup",
        "critical": True,
        "basin": "low",
        "margin": "critical",
        "nearest_edge": "coherence",
    }
    metrics = {
        "E": 0.42,
        "I": 0.38,
        "S": 0.41,
        "V": -0.18,
        "coherence": 0.31,
        "risk_score": 0.72,
        "phi": 0.24,
        "verdict": "high-risk",
        "void_active": False,
    }
    oscillation = SimpleNamespace(oi=0.0, flips=0, resonant=False, trigger=None)

    result = build_result(
        monitor,
        status="critical",
        decision=decision,
        metrics=metrics,
        confidence=0.67,
        confidence_metadata={"source": "external"},
        task_type_adjustment=None,
        trajectory_validation=None,
        oscillation_state=oscillation,
        response_tier="proceed",
        cirs_result=None,
        damping_result=None,
    )

    assert result["decision"] == decision
    assert result["policy_evaluation"] == {
        "policy_name": "monitor_decision",
        "policy_version": "v1",
        "action": "pause",
        "sub_action": "coherence_pause",
        "reason": "Coherence needs attention",
        "guidance": "Simplify and regroup",
        "inputs": {
            "basin": "low",
            "coherence": 0.31,
            "margin": "critical",
            "nearest_edge": "coherence",
            "phi": 0.24,
            "risk_score": 0.72,
            "verdict": "high-risk",
            "void_active": False,
        },
        "measurement_role": "EISV/risk/coherence are policy inputs, not the actuator itself.",
    }
    assert result["enforcement"] == {
        "requested": True,
        "applied": False,
        "mode": "circuit_breaker_candidate",
        "actor": None,
        "effect": None,
        "note": "Policy requested enforcement; actuator state is applied by the caller/runtime boundary.",
    }


def test_simulate_update_does_not_consume_novelty():
    """Council fold (PR #603): simulate_update runs the full governance
    cycle, including result building, which advances the novelty gate.
    The simulation must save/restore the gap so a dry run doesn't
    consume the agent's first REAL surfacing of the complexity line."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="test-sim-novelty-gap")
    assert monitor._last_surfaced_complexity_gap is None

    # Large reported-vs-derived gap: short text + complexity 0.9 is the
    # shape the live verifier reproduced (derived ≈ 0, Δ ≈ 0.9).
    agent_state = {"response_text": "ok", "complexity": 0.9}

    monitor.simulate_update(dict(agent_state))
    assert monitor._last_surfaced_complexity_gap is None, (
        "simulation burned the novelty gate — the first real check-in "
        "would silently lose the complexity line"
    )

    # The real path still engages the gate.
    monitor.process_update(dict(agent_state))
    assert monitor._last_surfaced_complexity_gap is not None
