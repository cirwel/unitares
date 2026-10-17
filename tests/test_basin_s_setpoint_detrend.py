"""The decision-path basin consumes the S setpoint the ODE and Φ use.

2026-10-15, contract rows 50 and 51. Stage A moved the ODE S attractor up by
the per-class setpoint σ and detrended Φ by σ (``src/monitor_setpoint.py``),
claiming verdict/basin invariance. The live basin (``config.classify_basin``)
still read raw ODE S against the fixed ``S_max = 0.25``, so the attractor move
put agents in BOUNDARY, and so to ``guide``, while verdict and risk stayed
put. Separately, the dynamics resolved σ through ``get_s_setpoint`` on a class
that can be ``None`` (σ = 0), while Φ resolved the same ``None`` to
``"default"``.

These tests pin the repair:

1. ``classify_basin`` evaluates the HIGH S bound on ``S - s_setpoint``; LOW has
   no S term; the default ``s_setpoint=0.0`` is the old classifier.
2. The ODE, Φ and the basin read one σ, through ``setpoint_for_monitor``,
   including for a class that resolves to ``None``.
3. The decision records the σ it used: in the policy-evaluation inputs and in
   the hard-stop provenance, and the cold-start guard's recomputation reads it.
"""
from __future__ import annotations

import uuid

import numpy as np
import pytest

from config.governance_config import BASIN_HIGH, classify_basin, get_s_setpoint
from src.monitor_setpoint import setpoint_for_monitor


DEFAULT_SIGMA = get_s_setpoint("default")


def _high_except_s(S):
    return dict(E=0.80, I=0.80, S=S, V=0.0, coherence=0.48, risk_score=0.20)


# ---------------------------------------------------------------------------
# 1. classify_basin
# ---------------------------------------------------------------------------


def test_default_sigma_is_positive_with_the_setpoint_on(monkeypatch):
    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    assert get_s_setpoint("default") > 0.1


def test_detrended_s_inside_the_bound_is_high_though_raw_s_is_not():
    raw_s = 0.30
    assert raw_s > BASIN_HIGH.S_max
    assert raw_s - DEFAULT_SIGMA <= BASIN_HIGH.S_max
    assert classify_basin(**_high_except_s(raw_s)) == "boundary"
    assert classify_basin(**_high_except_s(raw_s), s_setpoint=DEFAULT_SIGMA) == "high"


def test_s_above_the_detrended_bound_stays_boundary():
    raw_s = BASIN_HIGH.S_max + DEFAULT_SIGMA + 0.01
    assert classify_basin(**_high_except_s(raw_s), s_setpoint=DEFAULT_SIGMA) == "boundary"


def test_zero_setpoint_is_the_old_classifier():
    for s in (0.05, 0.20, 0.25, 0.26, 0.40):
        assert classify_basin(**_high_except_s(s)) == classify_basin(
            **_high_except_s(s), s_setpoint=0.0
        )


def test_the_setpoint_never_moves_low():
    low = dict(E=0.80, I=0.40, S=0.30, V=0.0, coherence=0.48, risk_score=0.20)
    assert classify_basin(**low) == "low"
    assert classify_basin(**low, s_setpoint=DEFAULT_SIGMA) == "low"
    risky = dict(_high_except_s(0.30), risk_score=0.75)
    assert classify_basin(**risky, s_setpoint=DEFAULT_SIGMA) == "low"


# ---------------------------------------------------------------------------
# 2. One σ for the ODE, Φ and the basin
# ---------------------------------------------------------------------------


def _monitor():
    from src.governance_monitor import UNITARESMonitor

    return UNITARESMonitor(f"test-basin-sigma-{uuid.uuid4().hex[:12]}", load_state=False)


def _ode_sigma(monkeypatch, monitor):
    import src.governance_monitor as GM

    seen = {}
    real = GM.step_state

    def spy(*args, **kwargs):
        seen["s_setpoint"] = kwargs.get("s_setpoint")
        return real(*args, **kwargs)

    monkeypatch.setattr(GM, "step_state", spy)
    monitor.update_dynamics({"complexity": 0.5})
    return seen["s_setpoint"]


def test_ode_sigma_equals_phi_sigma_for_a_class_that_resolves_to_none(monkeypatch):
    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    monitor = _monitor()
    monkeypatch.setattr(monitor, "_resolve_agent_class", lambda: None)

    ode_sigma = _ode_sigma(monkeypatch, monitor)

    assert monitor._resolved_agent_class is None
    assert ode_sigma == setpoint_for_monitor(monitor)
    assert ode_sigma == pytest.approx(DEFAULT_SIGMA)
    assert ode_sigma > 0.0


def test_a_cached_none_is_not_frozen_at_zero(monkeypatch):
    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    monitor = _monitor()
    monitor._resolved_agent_class = None  # e.g. an earlier metadata-cache miss
    monkeypatch.setattr(monitor, "_resolve_agent_class", lambda: "ephemeral")

    ode_sigma = _ode_sigma(monkeypatch, monitor)

    assert ode_sigma == pytest.approx(get_s_setpoint("ephemeral"))
    assert ode_sigma == setpoint_for_monitor(monitor)


def test_ode_sigma_is_zero_with_the_setpoint_off(monkeypatch):
    monkeypatch.setenv("UNITARES_S_SETPOINT", "0")
    monitor = _monitor()
    assert _ode_sigma(monkeypatch, monitor) == 0.0
    assert setpoint_for_monitor(monitor) == 0.0


def test_the_decision_classifies_the_basin_with_the_monitor_sigma(monkeypatch):
    import src.governance_monitor as GM

    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    monitor = _monitor()
    monkeypatch.setattr(monitor, "_resolve_agent_class", lambda: None)
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return {"action": "proceed", "sub_action": "approve", "basin": "high"}

    monkeypatch.setattr(GM, "_make_decision", capture)
    monitor.make_decision(0.2, unitares_verdict="safe")

    assert seen["s_setpoint"] == pytest.approx(DEFAULT_SIGMA)
    assert seen["latest_risk_fast_trip"] is True
    assert monitor._last_basin_s_setpoint == seen["s_setpoint"]


# ---------------------------------------------------------------------------
# 3. The σ is recorded where a replay reads it
# ---------------------------------------------------------------------------


def test_policy_evaluation_inputs_record_the_sigma_the_decision_used(monkeypatch):
    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    monitor = _monitor()
    result = monitor.process_update(
        {"parameters": np.array([]), "ethical_drift": [0.0, 0.0, 0.0],
         "response_text": "Ran the focused tests; they pass.", "complexity": 0.5},
        confidence=0.7,
    )
    recorded = result["policy_evaluation"]["inputs"]["s_setpoint"]
    assert recorded == monitor._last_basin_s_setpoint
    assert recorded == setpoint_for_monitor(monitor)


class _State:
    E, I, S, coherence = 0.80, 0.80, 0.30, 0.48
    V = 0.0
    void_active = False
    void_threshold_effective = 0.15
    coherence_role = None
    coherence_history_role = None

    def __init__(self):
        self.V_history = [0.0] * 20
        self.coherence_history = [0.48] * 6
        self.risk_history = [0.2]


def test_make_decision_detrends_the_basin_and_records_sigma_in_provenance():
    from src import monitor_decision as MD

    raw = MD.make_decision(_State(), risk_score=0.2, unitares_verdict="safe",
                           response_tier="proceed")
    detrended = MD.make_decision(_State(), risk_score=0.2, unitares_verdict="safe",
                                 response_tier="proceed", s_setpoint=DEFAULT_SIGMA)
    assert raw["basin"] == "boundary" and raw["sub_action"] == "guide"
    assert detrended["basin"] == "high" and detrended["sub_action"] == "approve"

    provenance = MD._build_hard_stop_provenance(
        state=_State(), risk_score=0.2, unitares_verdict="safe",
        response_tier="proceed", oscillation_state=None, cirs_result=None,
        basin="high", effective_coherence_threshold=0.40,
        s_setpoint=DEFAULT_SIGMA,
    )
    assert provenance["policy"]["observed"]["s_setpoint"] == DEFAULT_SIGMA
    assert provenance["policy"]["risk_neutral_counterfactual"]["basin"] == "high"


def _risk_pause_at_raw_s(s, **observed_extra):
    import copy

    from tests.test_cold_start_risk_confirmation import _risk_pause

    decision = copy.deepcopy(_risk_pause())
    policy = decision["hard_stop_provenance"]["policy"]
    policy["observed"]["S"] = s
    policy["observed"].update(observed_extra)
    return decision


def _candidate(decision):
    from tests.test_cold_start_risk_confirmation import _evaluate

    return _evaluate(decision=decision)["policy_candidate"]


def test_cold_start_recomputation_reads_the_recorded_sigma():
    # Raw S 0.30 is outside S_max, so without σ the risk-neutral basin would be
    # BOUNDARY; the producer recorded HIGH because it detrended by σ.
    assert _candidate(_risk_pause_at_raw_s(0.30, s_setpoint=DEFAULT_SIGMA)) is True


def test_a_record_without_sigma_is_recomputed_with_zero():
    # Historical records carry no σ and were classified with σ = 0, so a HIGH
    # counterfactual at raw S 0.30 is a contradiction and fails closed.
    assert _candidate(_risk_pause_at_raw_s(0.30)) is False
    assert _candidate(_risk_pause_at_raw_s(0.10)) is True


@pytest.mark.parametrize("bad", ["0.14", None, float("nan"), True])
def test_a_non_numeric_recorded_sigma_fails_closed(bad):
    assert _candidate(_risk_pause_at_raw_s(0.10, s_setpoint=bad)) is False
