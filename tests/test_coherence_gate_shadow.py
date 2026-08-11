"""Tests for the proprioceptive coherence gate shadow.

This is measurement-only code on the mandatory check-in path, so the properties
that matter most are: it never fires before a baseline is mature, it never
mutates a decision, and an ineligible agent is an explicit observation rather
than an absence that later reads as agreement.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coherence_gate_shadow import (
    K_BLOCK,
    K_FLOOR,
    K_PAUSE,
    coherence_gate_shadow_enabled,
    evaluate,
    record,
)


def _beh(z: float, baselined: bool = True):
    """Minimal BehavioralEISV stand-in: maturity flag + V z-score."""
    return SimpleNamespace(is_baselined=baselined, deviation=lambda dim: z)


# ── maturity gate ──────────────────────────────────────────────────────────


def test_immature_baseline_fires_nothing():
    # An agent whose dispersion cannot yet be estimated must not be gated
    # against it, however extreme the raw number looks.
    out = evaluate(_beh(-99.0, baselined=False), fleet_action="approve")
    assert out["eligible"] is False
    assert out["would_action"] is None
    assert out["v_zscore"] is None


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
])
def test_tier_boundaries(z, expected):
    assert evaluate(_beh(z), fleet_action="approve")["would_action"] == expected


def test_positive_deviation_never_fires():
    # Coherence ABOVE the agent's own normal is not a reason to gate it. A
    # symmetric threshold here would punish an agent for improving.
    for z in (1.0, 5.0, 50.0):
        assert evaluate(_beh(z), fleet_action="approve")["would_action"] == "proceed"


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
