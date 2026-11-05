"""Property-based algebraic invariants for the EISV behavioral sensor (SN1).

Verifies via Hypothesis that the default-ON ``SN1`` sensor posture
(``UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1``,
``UNITARES_PER_AGENT_I_CALIBRATION=1``) satisfies:

1. **Strict non-interference (zero self-loop derivative):** mutating controller
   and fleet loop inputs (``decision_history``, ``coherence_history``,
   ``regime_history``, shared ``calibration_error``, and server-generated
   ``trajectory_validated`` / ``cirs_resonance`` echo outcomes) produces zero
   change in ``E``, ``I``, ``S``, ``V``.
2. **Simplex conservation & range bounds:** for every combination of observed
   and missing inputs, base component weights sum to ``1.0`` and no neutralized
   self-loop or shared fleet-calibration component enters the base.
3. **Independent ``I`` evidence-gate truth table:** the four states of
   ``(calibration_eligible x outcomes_eligible)`` produce exact weights
   ``(5/7, 2/7)``, ``(1.0,)``, ``(1.0,)``, and ``uncalibrated_base_fallback=0.75``.
4. **Directional monotonicity:** worse ``complexity_divergence`` never raises
   ``E`` or lowers ``S``; higher ``drift_norm`` never lowers ``S``; higher
   eligible per-agent ``calibration_error`` never raises ``I``; better
   non-echo outcome success never lowers ``E``.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from src.behavioral_sensor import (
    compute_behavioral_sensor_components,
    compute_behavioral_sensor_eisv,
)

DECISION_VOCAB = ("approve", "proceed", "guide", "pause", "reject", "revise")
REGIME_VOCAB = ("STABLE", "CONVERGENCE", "EXPLORATION", "TRANSITION", "DIVERGENCE")
ECHO_TYPES = ("trajectory_validated", "cirs_resonance")
EXOGENOUS_TYPES = ("test_passed", "test_failed", "command_succeeded", "command_failed")

unit_float = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
opt_unit_float = st.one_of(st.none(), unit_float)
opt_velocity = st.one_of(
    st.none(),
    st.floats(min_value=0.0, max_value=25.0, allow_nan=False, allow_infinity=False),
)

controller_loop_state = st.fixed_dictionaries(
    {
        "decision_history": st.lists(st.sampled_from(DECISION_VOCAB), min_size=3, max_size=12),
        "coherence_history": st.lists(unit_float, min_size=3, max_size=12),
        "regime_history": st.lists(st.sampled_from(REGIME_VOCAB), min_size=0, max_size=12),
        "calibration_error": opt_unit_float,
        "echo_outcomes": st.lists(
            st.fixed_dictionaries(
                {
                    "outcome_type": st.sampled_from(ECHO_TYPES),
                    "is_bad": st.booleans(),
                    "outcome_score": unit_float,
                }
            ),
            min_size=0,
            max_size=5,
        ),
    }
)

exogenous_outcome_strategy = st.fixed_dictionaries(
    {
        "outcome_type": st.sampled_from(EXOGENOUS_TYPES),
        "is_bad": st.booleans(),
        "outcome_score": unit_float,
    }
)

agent_cal_channel_strategy = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "evidence_status": st.sampled_from(["available", "unavailable"]),
            "freshness_status": st.sampled_from(["recent", "stale", "unknown"]),
            "n_verified_pairs": st.integers(min_value=0, max_value=30),
            "calibration_error": opt_unit_float,
            "prior_error": st.one_of(
                st.none(),
                st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False),
            ),
        }
    ),
)


@given(
    loop_a=controller_loop_state,
    loop_b=controller_loop_state,
    e_hist=st.lists(unit_float, min_size=0, max_size=10),
    i_hist=st.lists(unit_float, min_size=0, max_size=10),
    drift_norm=opt_unit_float,
    complexity_divergence=opt_unit_float,
    continuity_E=opt_unit_float,
    continuity_I=opt_unit_float,
    continuity_S=opt_unit_float,
    exogenous_outcomes=st.lists(exogenous_outcome_strategy, min_size=0, max_size=8),
    tool_error_rate=opt_unit_float,
    tool_call_velocity=opt_velocity,
    unique_tools_ratio=opt_unit_float,
    agent_cal=agent_cal_channel_strategy,
)
@settings(max_examples=120, deadline=None)
def test_sn1_non_interference_of_controller_and_fleet_loops(
    loop_a,
    loop_b,
    e_hist,
    i_hist,
    drift_norm,
    complexity_divergence,
    continuity_E,
    continuity_I,
    continuity_S,
    exogenous_outcomes,
    tool_error_rate,
    tool_call_velocity,
    unique_tools_ratio,
    agent_cal,
):
    """Mutating controller/fleet self-loop inputs changes E, I, S, V by 0.0 under SN1."""
    common = dict(
        E_history=e_hist,
        I_history=i_hist,
        S_history=[],
        V_history=[],
        drift_norm=drift_norm,
        complexity_divergence=complexity_divergence,
        continuity_E_input=continuity_E,
        continuity_I_input=continuity_I,
        continuity_S_input=continuity_S,
        tool_error_rate=tool_error_rate,
        tool_call_velocity=tool_call_velocity,
        unique_tools_ratio=unique_tools_ratio,
        agent_calibration_channel=agent_cal,
        neutralize_self_loops=True,
        per_agent_i_calibration=True,
    )

    obs_a = compute_behavioral_sensor_eisv(
        decision_history=loop_a["decision_history"],
        coherence_history=loop_a["coherence_history"],
        regime_history=loop_a["regime_history"],
        calibration_error=loop_a["calibration_error"],
        outcome_history=exogenous_outcomes + loop_a["echo_outcomes"],
        **common,
    )
    obs_b = compute_behavioral_sensor_eisv(
        decision_history=loop_b["decision_history"],
        coherence_history=loop_b["coherence_history"],
        regime_history=loop_b["regime_history"],
        calibration_error=loop_b["calibration_error"],
        outcome_history=exogenous_outcomes + loop_b["echo_outcomes"],
        **common,
    )

    assert obs_a is not None and obs_b is not None
    assert obs_a == obs_b


@given(
    drift_norm=opt_unit_float,
    complexity_divergence=opt_unit_float,
    continuity_E=opt_unit_float,
    continuity_I=opt_unit_float,
    continuity_S=opt_unit_float,
    exogenous_outcomes=st.lists(exogenous_outcome_strategy, min_size=0, max_size=8),
    tool_error_rate=opt_unit_float,
    tool_call_velocity=opt_velocity,
    unique_tools_ratio=opt_unit_float,
    agent_cal=agent_cal_channel_strategy,
)
@settings(max_examples=120, deadline=None)
def test_sn1_base_weight_simplex_and_excluded_loop_sources(
    drift_norm,
    complexity_divergence,
    continuity_E,
    continuity_I,
    continuity_S,
    exogenous_outcomes,
    tool_error_rate,
    tool_call_velocity,
    unique_tools_ratio,
    agent_cal,
):
    """Base component weights always sum to 1.0 and exclude all self-loop/fleet terms."""
    record = compute_behavioral_sensor_components(
        decision_history=["approve", "guide", "pause"],
        coherence_history=[0.42, 0.50, 0.58],
        regime_history=["STABLE", "EXPLORATION", "DIVERGENCE"],
        E_history=[0.7, 0.7, 0.7],
        I_history=[0.7, 0.7, 0.7],
        S_history=[0.2, 0.2, 0.2],
        V_history=[0.0, 0.0, 0.0],
        calibration_error=0.25,
        drift_norm=drift_norm,
        complexity_divergence=complexity_divergence,
        continuity_E_input=continuity_E,
        continuity_I_input=continuity_I,
        continuity_S_input=continuity_S,
        outcome_history=exogenous_outcomes,
        tool_error_rate=tool_error_rate,
        tool_call_velocity=tool_call_velocity,
        unique_tools_ratio=unique_tools_ratio,
        agent_calibration_channel=agent_cal,
        neutralize_self_loops=True,
        per_agent_i_calibration=True,
    )
    assert record is not None
    forbidden_names = {
        "decision_history",
        "legacy_coherence_level",
        "legacy_coherence_trend",
        "regime_instability",
    }
    for dim in ("E", "I", "S"):
        d = record["dimensions"][dim]
        comps = d["components"]
        assert comps, f"{dim} must have at least one base component"
        weight_sum = sum(c["weight"] for c in comps)
        assert weight_sum == pytest.approx(1.0, abs=1e-9)
        for c in comps:
            assert 0.0 < c["weight"] <= 1.0
            assert c["name"] not in forbidden_names
            assert c["source"] != "fleet_calibration"
        assert 0.0 <= d["base_value"] <= 1.0
    assert 0.05 <= record["dimensions"]["S"]["base_value"] <= 1.0
    assert 0.0 <= record["dimensions"]["E"]["value"] <= 1.0
    assert 0.0 <= record["dimensions"]["I"]["value"] <= 1.0
    assert -1.0 <= record["dimensions"]["V"]["value"] <= 1.0


@given(
    cal_eligible=st.booleans(),
    outcomes_eligible=st.booleans(),
    agent_err=unit_float,
    scores=st.lists(unit_float, min_size=3, max_size=6),
)
@settings(max_examples=80, deadline=None)
def test_sn1_i_independent_evidence_gates_truth_table(
    cal_eligible,
    outcomes_eligible,
    agent_err,
    scores,
):
    """All four (calibration_eligible x outcomes_eligible) states match the SN1 contract."""
    channel = (
        {
            "evidence_status": "available",
            "freshness_status": "recent",
            "n_verified_pairs": 6,
            "calibration_error": agent_err,
            "prior_error": None,
        }
        if cal_eligible
        else {
            "evidence_status": "unavailable",
            "freshness_status": "unknown",
            "n_verified_pairs": 0,
            "calibration_error": None,
            "prior_error": None,
        }
    )
    outcomes = (
        [
            {"outcome_type": "test_passed", "is_bad": s < 0.5, "outcome_score": s}
            for s in scores
        ]
        if outcomes_eligible
        else [
            {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
            {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
        ]
    )
    record = compute_behavioral_sensor_components(
        decision_history=["approve", "approve", "approve"],
        coherence_history=[0.5, 0.5, 0.5],
        regime_history=["STABLE", "STABLE", "STABLE"],
        E_history=[0.7, 0.7, 0.7],
        I_history=[0.7, 0.7, 0.7],
        S_history=[0.2, 0.2, 0.2],
        V_history=[0.0, 0.0, 0.0],
        calibration_error=0.40,
        outcome_history=outcomes,
        agent_calibration_channel=channel,
        neutralize_self_loops=True,
        per_agent_i_calibration=True,
    )
    i_dim = record["dimensions"]["I"]
    comps = i_dim["components"]

    mean_s = sum(scores) / len(scores)
    var_s = sum((s - mean_s) ** 2 for s in scores) / len(scores)
    expected_consistency = max(0.3, 1.0 - var_s * 4.0)
    expected_cal = 1.0 - agent_err

    if cal_eligible and outcomes_eligible:
        assert [c["name"] for c in comps] == [
            "calibration_accuracy",
            "outcome_score_consistency",
        ]
        assert comps[0]["weight"] == pytest.approx(5.0 / 7.0)
        assert comps[1]["weight"] == pytest.approx(2.0 / 7.0)
        assert i_dim["value"] == pytest.approx(
            (5.0 / 7.0) * expected_cal + (2.0 / 7.0) * expected_consistency
        )
    elif cal_eligible and not outcomes_eligible:
        assert [c["name"] for c in comps] == ["calibration_accuracy"]
        assert comps[0]["weight"] == pytest.approx(1.0)
        assert i_dim["value"] == pytest.approx(expected_cal)
    elif not cal_eligible and outcomes_eligible:
        assert [c["name"] for c in comps] == ["outcome_score_consistency"]
        assert comps[0]["weight"] == pytest.approx(1.0)
        assert i_dim["value"] == pytest.approx(expected_consistency)
    else:
        assert [c["name"] for c in comps] == ["uncalibrated_base_fallback"]
        assert comps[0]["weight"] == pytest.approx(1.0)
        assert comps[0]["observed"] is False
        assert i_dim["value"] == pytest.approx(0.75)


@given(
    low_val=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
    drift=unit_float,
    comp_div=unit_float,
)
@settings(max_examples=80, deadline=None)
def test_sn1_directional_monotonicity(low_val, delta, drift, comp_div):
    """Worse drift/divergence/calibration/outcomes move E, I, S in the expected direction."""
    high_val = min(1.0, low_val + delta)
    base_kwargs = dict(
        decision_history=["approve", "approve", "approve"],
        coherence_history=[0.5, 0.5, 0.5],
        regime_history=["STABLE", "STABLE", "STABLE"],
        E_history=[0.7, 0.7, 0.7],
        I_history=[0.7, 0.7, 0.7],
        S_history=[0.2, 0.2, 0.2],
        V_history=[0.0, 0.0, 0.0],
        neutralize_self_loops=True,
        per_agent_i_calibration=True,
    )

    # 1. Higher complexity_divergence never raises E and never lowers S
    low_cd = compute_behavioral_sensor_eisv(
        **base_kwargs, drift_norm=drift, complexity_divergence=low_val
    )
    high_cd = compute_behavioral_sensor_eisv(
        **base_kwargs, drift_norm=drift, complexity_divergence=high_val
    )
    assert high_cd["E"] <= low_cd["E"] + 1e-12
    assert high_cd["S"] >= low_cd["S"] - 1e-12

    # 2. Higher drift_norm never lowers S
    low_dn = compute_behavioral_sensor_eisv(
        **base_kwargs, drift_norm=low_val, complexity_divergence=comp_div
    )
    high_dn = compute_behavioral_sensor_eisv(
        **base_kwargs, drift_norm=high_val, complexity_divergence=comp_div
    )
    assert high_dn["S"] >= low_dn["S"] - 1e-12

    # 3. Higher per-agent calibration_error never raises I
    low_err_cal = {
        "evidence_status": "available",
        "freshness_status": "recent",
        "n_verified_pairs": 6,
        "calibration_error": low_val,
    }
    high_err_cal = {
        "evidence_status": "available",
        "freshness_status": "recent",
        "n_verified_pairs": 6,
        "calibration_error": high_val,
    }
    low_err_obs = compute_behavioral_sensor_eisv(
        **base_kwargs, agent_calibration_channel=low_err_cal
    )
    high_err_obs = compute_behavioral_sensor_eisv(
        **base_kwargs, agent_calibration_channel=high_err_cal
    )
    assert high_err_obs["I"] <= low_err_obs["I"] + 1e-12

    # 4. Better outcome success rate never lowers E
    worse_outcomes = [
        {"outcome_type": "test_failed", "is_bad": True, "outcome_score": 0.0},
        {"outcome_type": "test_failed", "is_bad": True, "outcome_score": 0.0},
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
    ]
    better_outcomes = [
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
        {"outcome_type": "test_passed", "is_bad": False, "outcome_score": 1.0},
    ]
    worse_e = compute_behavioral_sensor_eisv(
        **base_kwargs, complexity_divergence=comp_div, outcome_history=worse_outcomes
    )
    better_e = compute_behavioral_sensor_eisv(
        **base_kwargs, complexity_divergence=comp_div, outcome_history=better_outcomes
    )
    assert better_e["E"] >= worse_e["E"] - 1e-12
