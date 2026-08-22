"""Regression contract for cold-start risk confirmation shadow telemetry."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from governance_core.adaptive_governor import AdaptiveGovernor
from src.cirs import OscillationState
from src.cold_start_risk_confirmation import (
    COLD_START_CONFIRMATION_ACTUATION_SCOPE,
    NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS,
    NON_AUTHORED_COLD_START_RECOVERY_BASIS,
    apply_non_authored_cold_start_guard,
    classify_verdict_driver,
    evaluate_cold_start_risk_confirmation,
    evaluate_non_authored_cold_start_trap,
)
from src.monitor_decision import HARD_STOP_PROVENANCE_SCHEMA, make_decision


def _risk_only_hard_stop_provenance(*, sub_action, nearest_edge, cirs=False):
    risk_score = 0.85 if cirs else 0.80
    risk_hard_stops = (
        ["cirs_risk_ceiling", "high_risk_verdict", "basin_risk_floor"]
        if cirs
        else ["high_risk_verdict", "basin_risk_floor"]
    )
    return {
        "schema": HARD_STOP_PROVENANCE_SCHEMA,
        "complete": True,
        "risk_only": True,
        "risk_hard_stops": risk_hard_stops,
        "independent_hard_stops": [],
        "cirs": {
            "mode": "adaptive_v2" if cirs else "not_supplied",
            "response_tier": "hard_block" if cirs else None,
            "provenance_complete": True,
            "observed": {
                "coherence": 0.6,
                "risk_score": risk_score,
                "oscillation_index": 0.0 if cirs else None,
                "flips": 0 if cirs else None,
            },
            "thresholds": {
                "coherence_floor": 0.25 if cirs else None,
                "risk_ceiling": 0.80 if cirs else None,
                "oscillation_index": 2.5 if cirs else None,
                "flips": 4 if cirs else None,
            },
            "conditions": {
                "coherence_floor": False,
                "risk_ceiling": cirs,
                "resonance": False,
                "unclassified_hard_block": False,
            },
        },
        "policy": {
            "observed": {
                "E": 0.8,
                "I": 0.8,
                "S": 0.1,
                "V": 0.0,
                "coherence": 0.6,
                "risk_score": risk_score,
                "void_active": False,
                "verdict": "high-risk",
                "basin": "low",
            },
            "thresholds": {
                "coherence_critical": 0.4,
                "basin_low_I": 0.5,
                "basin_low_coherence": 0.4,
                "basin_low_abs_V": 0.3,
                "basin_low_risk": 0.7,
            },
            "conditions": {
                "void_active": False,
                "coherence_floor": False,
                "high_risk_verdict": True,
                "low_basin": True,
                "basin_low_integrity": False,
                "basin_low_coherence": False,
                "basin_high_abs_valence": False,
                "basin_risk_floor": True,
                "independent_low_basin": False,
            },
            "risk_neutral_counterfactual": {
                "risk_score": 0.0,
                "basin": "high",
            },
        },
        "selected_decision": {
            "action": "pause",
            "sub_action": sub_action,
            "nearest_edge": nearest_edge,
        },
    }


def _risk_pause():
    return {
        "action": "pause",
        "sub_action": "risk_pause",
        "reason": "UNITARES high-risk verdict",
        "nearest_edge": "risk",
        "hard_stop_provenance": _risk_only_hard_stop_provenance(
            sub_action="risk_pause",
            nearest_edge="risk",
        ),
    }


def _cirs_block(nearest_edge="risk"):
    return {
        "action": "pause",
        "sub_action": "cirs_block",
        "reason": "CIRS risk ceiling breached",
        "guidance": "Pause to investigate the risk spike.",
        "nearest_edge": nearest_edge,
        "hard_stop_provenance": _risk_only_hard_stop_provenance(
            sub_action="cirs_block",
            nearest_edge=nearest_edge,
            cirs=True,
        ),
    }


def _full_stack_cirs_risk_decision(
    *,
    risk_score=0.85,
    coherence=0.6,
    integrity=0.8,
    void_active=False,
    resonant=False,
    omit_cirs_provenance=False,
):
    state = SimpleNamespace(
        E=0.8,
        I=integrity,
        S=0.1,
        V=0.0,
        coherence=coherence,
        void_active=void_active,
        coherence_history=[],
        risk_history=[],
    )
    governor = AdaptiveGovernor()
    cirs_result = governor.update(
        coherence=coherence,
        risk=risk_score,
        verdict="high-risk",
        E_history=[0.8] * 6,
        I_history=[integrity] * 6,
        S_history=[0.1] * 6,
        complexity_history=[0.3] * 6,
        V_history=[0.0] * 6,
    )
    if resonant:
        cirs_result["oi"] = 2.5
        cirs_result["resonant"] = True
        cirs_result["trigger"] = "oi"
        cirs_provenance = cirs_result["hard_stop_provenance"]
        cirs_provenance["observed"]["oscillation_index"] = 2.5
        cirs_provenance["conditions"]["resonance"] = True
    if omit_cirs_provenance:
        cirs_result.pop("hard_stop_provenance")
    oscillation = OscillationState(
        oi=cirs_result["oi"],
        flips=cirs_result["flips"],
        resonant=cirs_result["resonant"],
        trigger=cirs_result["trigger"],
    )
    return make_decision(
        state,
        risk_score,
        unitares_verdict="high-risk",
        response_tier=cirs_result["verdict"],
        oscillation_state=oscillation,
        cirs_result=cirs_result,
    )


def _evaluate(*, decision=None, previous=None, cycle=1, **overrides):
    arguments = {
        "behavioral_confidence": 0.1,
        "is_baselined": False,
        "primary_driver": "phi_cold_start",
        "primary_eisv_source": "ode_fallback",
        "process_cycle": cycle,
        "monitor_lineage": "lineage-a",
        "lineage_status": "identity_genesis",
        "previous_evaluation": previous,
        "history_gap": False,
        "independent_override": None,
        "shadow_enabled": True,
        "actuation_enabled": False,
    }
    arguments.update(overrides)
    evaluated_decision = _risk_pause() if decision is None else decision
    return evaluate_cold_start_risk_confirmation(evaluated_decision, **arguments)


def _decision_with_gate(decision=None, **gate_overrides):
    guarded_decision = dict(_risk_pause() if decision is None else decision)
    guarded_decision["cold_start_confirmation"] = _evaluate(
        decision=guarded_decision,
        **gate_overrides,
    )
    return guarded_decision


def _legacy_trap_record(
    *,
    epistemic_class="substrate_interpretation",
    enforcement_basis="phi_cold_start_unconfirmed_shadow",
    maturity_gate=None,
):
    maturity_gate = maturity_gate or _evaluate()
    return SimpleNamespace(
        epistemic_class=epistemic_class,
        state_json={
            "epistemic_class": epistemic_class,
            "eisv_telemetry": {
                "schema": "eisv.telemetry.v1",
                "policy_evaluation": {
                    "action": "pause",
                    "sub_action": "risk_pause",
                    "hard_stop_provenance": maturity_gate[
                        "hard_stop_provenance"
                    ],
                    "inputs": {
                        "verdict_source": "phi_cold_start",
                        "primary_eisv_source": "ode_fallback",
                        "nearest_edge": "risk",
                    },
                    "maturity_gate": maturity_gate,
                },
                "enforcement": {
                    "requested": True,
                    "applied": True,
                    "mode": "circuit_breaker",
                    "basis": enforcement_basis,
                    "scope": "runtime_circuit_breaker",
                    "actor": "agent_loop_detection",
                    "effect": "agent_metadata.status=paused",
                    "actuation_id": "actuation-123",
                    "applied_at": "2026-08-11T23:47:55+00:00",
                },
            },
        },
    )


def test_first_fallback_risk_pause_is_shadow_would_defer_only():
    gate = _evaluate()

    assert gate["eligible"] is True
    assert gate["confirmation_count"] == 1
    assert gate["confirmations_required"] == 2
    assert gate["would_defer"] is True
    assert gate["outcome"] == "shadow_would_defer"
    assert gate["actuation_scope"] == COLD_START_CONFIRMATION_ACTUATION_SCOPE
    assert gate["actuation_applied"] is False
    assert "runtime circuit breaker" in gate["note"]
    original = gate["original_decision"]
    assert original["action"] == "pause"
    assert original["sub_action"] == "risk_pause"
    assert original["reason"] == "UNITARES high-risk verdict"
    assert original["nearest_edge"] == "risk"
    assert original["hard_stop_provenance"]["risk_only"] is True


def test_risk_attributed_cirs_block_is_the_same_fallback_risk_candidate():
    gate = _evaluate(decision=_cirs_block())

    assert gate["policy_candidate"] is True
    assert gate["eligible"] is True
    assert gate["would_defer"] is True
    assert gate["enforcement_basis"] == "phi_cold_start_unconfirmed_shadow"
    original = gate["original_decision"]
    assert original["action"] == "pause"
    assert original["sub_action"] == "cirs_block"
    assert original["reason"] == "CIRS risk ceiling breached"
    assert original["nearest_edge"] == "risk"
    assert original["hard_stop_provenance"]["risk_only"] is True


@pytest.mark.parametrize(
    ("decision", "expected_candidate"),
    [
        (_risk_pause(), True),
        (_cirs_block("risk"), True),
        (_cirs_block("oscillation"), False),
        (_cirs_block("coherence"), False),
        (_cirs_block(None), False),
        (_cirs_block("unclassified"), False),
        (
            {
                "action": "pause",
                "sub_action": "system_maintenance",
                "reason": "maintenance window",
            },
            False,
        ),
        (
            {
                "action": "proceed",
                "sub_action": "risk_pause",
                "reason": "not a pause",
            },
            False,
        ),
    ],
)
def test_fallback_risk_candidate_truth_table(decision, expected_candidate):
    gate = _evaluate(decision=decision)

    assert gate["policy_candidate"] is expected_candidate
    if not expected_candidate:
        assert gate["ineligibility_reason"] == "policy_not_risk_pause"
        assert gate["eligible"] is False


@pytest.mark.parametrize(
    ("decision", "sub_action", "nearest_edge"),
    [
        (_risk_pause(), "cirs_block", "risk"),
        (_cirs_block(), "risk_pause", "risk"),
        (_risk_pause(), "risk_pause", "coherence"),
    ],
)
def test_impossible_route_fails_closed_even_when_record_is_self_consistent(
    decision,
    sub_action,
    nearest_edge,
):
    decision = deepcopy(decision)
    decision["sub_action"] = sub_action
    decision["nearest_edge"] = nearest_edge
    selected = decision["hard_stop_provenance"]["selected_decision"]
    selected["sub_action"] = sub_action
    selected["nearest_edge"] = nearest_edge

    gate = _evaluate(decision=decision)
    decision["cold_start_confirmation"] = gate
    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert gate["policy_candidate"] is False
    assert guarded["action"] == "pause"
    assert guarded.get("cold_start_epistemic_deferred") is not True
    if "cold_start_epistemic_gate" in guarded:
        assert guarded["cold_start_epistemic_gate"]["applied"] is False


def test_live_adaptive_cirs_risk_ceiling_records_exact_risk_only_provenance():
    decision = _full_stack_cirs_risk_decision()
    provenance = decision["hard_stop_provenance"]

    assert decision["sub_action"] == "cirs_block"
    assert decision["nearest_edge"] == "risk"
    assert "> 0.80" in decision["reason"]
    assert provenance["complete"] is True
    assert provenance["risk_only"] is True
    assert provenance["risk_hard_stops"] == [
        "cirs_risk_ceiling",
        "high_risk_verdict",
        "basin_risk_floor",
    ]
    assert provenance["independent_hard_stops"] == []
    assert provenance["cirs"]["thresholds"] == {
        "coherence_floor": 0.25,
        "risk_ceiling": 0.8,
        "oscillation_index": 2.5,
        "flips": 4,
    }
    assert provenance["policy"]["risk_neutral_counterfactual"] == {
        "risk_score": 0.0,
        "basin": "high",
    }
    assert _evaluate(decision=decision)["policy_candidate"] is True


def test_live_adaptive_non_blocking_tier_can_reach_direct_risk_pause_safely():
    decision = _full_stack_cirs_risk_decision(risk_score=0.75)
    provenance = decision["hard_stop_provenance"]

    assert decision["sub_action"] == "risk_pause"
    assert provenance["cirs"]["response_tier"] == "high-risk"
    assert provenance["cirs"]["conditions"] == {
        "coherence_floor": False,
        "risk_ceiling": False,
        "resonance": False,
        "unclassified_hard_block": False,
    }
    assert provenance["risk_only"] is True
    assert _evaluate(decision=decision)["policy_candidate"] is True


@pytest.mark.parametrize(
    ("overrides", "expected_independent_stop"),
    [
        ({"resonant": True}, "cirs_resonance"),
        ({"coherence": 0.2}, "cirs_coherence_floor"),
        ({"void_active": True}, "void_active"),
        ({"integrity": 0.4}, "independent_low_basin"),
        ({"omit_cirs_provenance": True}, "cirs_unclassified_hard_block"),
    ],
)
def test_simultaneous_or_unclassified_hard_stops_never_defer(
    overrides,
    expected_independent_stop,
):
    decision = _full_stack_cirs_risk_decision(**overrides)
    provenance = decision["hard_stop_provenance"]
    gate = _evaluate(decision=decision)
    decision["cold_start_confirmation"] = gate

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert expected_independent_stop in provenance["independent_hard_stops"]
    assert provenance["risk_only"] is False
    assert gate["policy_candidate"] is False
    assert guarded["action"] == "pause"
    assert guarded["sub_action"] == decision["sub_action"]


def test_missing_hard_stop_provenance_fails_closed():
    decision = _risk_pause()
    decision.pop("hard_stop_provenance")

    gate = _evaluate(decision=decision)
    decision["cold_start_confirmation"] = gate
    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert gate["policy_candidate"] is False
    assert gate["ineligibility_reason"] == (
        "hard_stop_provenance_missing_or_not_risk_only"
    )
    assert guarded["action"] == "pause"
    assert guarded["cold_start_epistemic_gate"]["applied"] is False
    assert guarded["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "hard_stop_provenance_missing_or_not_risk_only"
    )


def test_primary_eisv_source_and_operator_override_are_exact_fail_closed_gates():
    wrong_source = _evaluate(primary_eisv_source="behavioral")
    operator_override = _evaluate(independent_override="operator_override")
    already_baselined = _evaluate(is_baselined=True)

    assert wrong_source["eligible"] is False
    assert wrong_source["ineligibility_reason"] == "eisv_source_not_ode_fallback"
    assert operator_override["eligible"] is False
    assert operator_override["ineligibility_reason"] == "independent_override"
    assert already_baselined["eligible"] is False
    assert already_baselined["ineligibility_reason"] == "behavioral_baseline_present"


_HARD_STOP_FAIL_CLOSED_MUTATIONS = [
    (("schema",), "unknown"),
    (("complete",), False),
    (("risk_only",), False),
    (("risk_hard_stops",), []),
    (("independent_hard_stops",), ["void_active"]),
    (("selected_decision", "action"), "proceed"),
    (("selected_decision", "sub_action"), "guide"),
    (("selected_decision", "nearest_edge"), None),
    (("cirs", "provenance_complete"), False),
    (("cirs", "mode"), "unknown"),
    (("cirs", "conditions", "coherence_floor"), None),
    (("cirs", "conditions", "risk_ceiling"), None),
    (("cirs", "conditions", "resonance"), None),
    (("cirs", "conditions", "unclassified_hard_block"), None),
] + [
    (("policy", "observed", field), None)
    for field in (
        "E",
        "I",
        "S",
        "V",
        "coherence",
        "risk_score",
        "void_active",
        "verdict",
        "basin",
    )
] + [
    (("policy", "thresholds", field), None)
    for field in (
        "coherence_critical",
        "basin_low_I",
        "basin_low_coherence",
        "basin_low_abs_V",
        "basin_low_risk",
    )
] + [
    (("policy", "conditions", field), None)
    for field in (
        "void_active",
        "coherence_floor",
        "high_risk_verdict",
        "low_basin",
        "basin_low_integrity",
        "basin_low_coherence",
        "basin_high_abs_valence",
        "basin_risk_floor",
        "independent_low_basin",
    )
] + [
    (("policy", "risk_neutral_counterfactual", "risk_score"), None),
    (("policy", "risk_neutral_counterfactual", "basin"), None),
]

_CIRS_FAIL_CLOSED_MUTATIONS = [
    (("cirs", "response_tier"), "unknown"),
] + [
    (("cirs", "observed", field), None)
    for field in (
        "coherence",
        "risk_score",
        "oscillation_index",
        "flips",
    )
] + [
    (("cirs", "thresholds", field), None)
    for field in (
        "coherence_floor",
        "risk_ceiling",
        "oscillation_index",
        "flips",
    )
]


@pytest.mark.parametrize(("path", "replacement"), _HARD_STOP_FAIL_CLOSED_MUTATIONS)
def test_each_hard_stop_provenance_field_fails_closed_when_contradictory(
    path,
    replacement,
):
    decision = _decision_with_gate()
    mutated = deepcopy(decision["hard_stop_provenance"])
    target = mutated
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    decision["hard_stop_provenance"] = mutated
    decision["cold_start_confirmation"]["hard_stop_provenance"] = deepcopy(
        mutated
    )

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "pause"
    assert guarded["sub_action"] == "risk_pause"
    assert guarded["cold_start_epistemic_gate"]["applied"] is False


@pytest.mark.parametrize(("path", "replacement"), _CIRS_FAIL_CLOSED_MUTATIONS)
def test_each_cirs_provenance_field_fails_closed_when_missing_or_unknown(
    path,
    replacement,
):
    decision = _decision_with_gate(_cirs_block())
    mutated = deepcopy(decision["hard_stop_provenance"])
    target = mutated
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    decision["hard_stop_provenance"] = mutated
    decision["cold_start_confirmation"]["hard_stop_provenance"] = deepcopy(
        mutated
    )

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "pause"
    assert guarded["sub_action"] == "cirs_block"
    assert guarded["cold_start_epistemic_gate"]["applied"] is False


@pytest.mark.parametrize(
    ("threshold", "replacement"),
    [
        ("coherence_floor", 0.31),
        ("risk_ceiling", 0.71),
        ("oscillation_index", 3.1),
        ("flips", 4),
    ],
)
def test_legacy_cirs_fixed_thresholds_reject_coherent_relabeling(
    threshold,
    replacement,
):
    state = SimpleNamespace(
        E=0.8,
        I=0.8,
        S=0.1,
        V=0.0,
        coherence=0.6,
        void_active=False,
        coherence_history=[],
        risk_history=[],
    )
    decision = make_decision(
        state,
        risk_score=0.75,
        unitares_verdict="high-risk",
        response_tier="hard_block",
        oscillation_state=OscillationState(),
    )
    assert _evaluate(decision=decision)["policy_candidate"] is True
    decision = _decision_with_gate(decision)
    mutated = deepcopy(decision["hard_stop_provenance"])
    mutated["cirs"]["thresholds"][threshold] = replacement
    decision["hard_stop_provenance"] = mutated
    decision["cold_start_confirmation"]["hard_stop_provenance"] = deepcopy(
        mutated
    )

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "pause"
    assert guarded["cold_start_epistemic_gate"]["applied"] is False


@pytest.mark.parametrize(
    "missing_field",
    [
        "schema",
        "policy_candidate",
        "provenance_complete",
        "hard_stop_provenance",
        "primary_driver",
        "primary_eisv_source",
        "measurement_ready",
        "is_baselined",
        "behavioral_confidence",
        "independent_override",
    ],
)
def test_each_missing_maturity_authority_field_fails_closed(missing_field):
    decision = _decision_with_gate()
    decision["cold_start_confirmation"].pop(missing_field)

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "pause"
    assert guarded["cold_start_epistemic_gate"]["applied"] is False
    assert guarded["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "maturity_provenance_incomplete"
    )


def test_guard_is_a_pure_transition_and_never_mutates_original_pause():
    decision = _decision_with_gate()
    original = deepcopy(decision)

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert decision == original
    assert decision["action"] == "pause"
    assert guarded["action"] == "proceed"
    assert guarded["original_action"] == "pause"


def test_second_adjacent_fallback_risk_pause_is_shadow_confirmed():
    first = _evaluate()
    second = _evaluate(previous=first, cycle=2)

    assert second["eligible"] is True
    assert second["confirmation_count"] == 2
    assert second["would_defer"] is False
    assert second["confirmed"] is True
    assert second["outcome"] == "shadow_confirmed"
    assert second["enforcement_basis"] == "phi_cold_start_confirmed"


def test_intervening_observation_resets_confirmation_count():
    first = _evaluate()
    intervening = _evaluate(
        previous=first,
        cycle=2,
        behavioral_confidence=0.3,
        primary_driver="behavioral_assessment",
    )
    third = _evaluate(previous=intervening, cycle=3)

    assert intervening["eligible"] is False
    assert intervening["ineligibility_reason"] == "behavioral_measurement_ready"
    assert third["confirmation_count"] == 1
    assert third["reset_reason"] == "intervening_or_discontinuous_observation"


def test_gap_restart_and_incomplete_provenance_fail_closed():
    gap = _evaluate(history_gap=True)
    restart = _evaluate(lineage_status="restored_snapshot")
    incomplete = _evaluate(behavioral_confidence=None)
    missing_lineage = _evaluate(monitor_lineage="")
    invalid_cycle = _evaluate(cycle=0)

    assert gap["ineligibility_reason"] == "history_gap"
    assert restart["ineligibility_reason"] == "restart_or_lineage_uncertainty"
    assert incomplete["ineligibility_reason"] == "provenance_incomplete"
    assert missing_lineage["ineligibility_reason"] == "provenance_incomplete"
    assert invalid_cycle["ineligibility_reason"] == "provenance_incomplete"
    assert gap["would_defer"] is False
    assert restart["would_defer"] is False
    assert incomplete["would_defer"] is False
    assert missing_lineage["would_defer"] is False
    assert invalid_cycle["would_defer"] is False


def test_missing_original_reason_is_incomplete_provenance():
    decision = _risk_pause()
    decision.pop("reason")

    gate = evaluate_cold_start_risk_confirmation(
        decision,
        behavioral_confidence=0.1,
        is_baselined=False,
        primary_driver="phi_cold_start",
        primary_eisv_source="ode_fallback",
        process_cycle=1,
        monitor_lineage="lineage-a",
        lineage_status="identity_genesis",
        previous_evaluation=None,
        history_gap=False,
        independent_override=None,
        shadow_enabled=True,
        actuation_enabled=False,
    )

    assert gate["provenance_complete"] is False
    assert gate["ineligibility_reason"] == "provenance_incomplete"
    assert gate["would_defer"] is False


def test_independent_verification_override_is_never_confirmation_eligible():
    gate = _evaluate(
        primary_driver="independent_verification_floor",
        independent_override="independent_verification_floor",
    )

    assert gate["eligible"] is False
    assert gate["ineligibility_reason"] == "independent_override"
    assert gate["enforcement_basis"] == "independent_override"


def test_non_risk_pause_and_disabled_shadow_are_inert():
    non_risk = evaluate_cold_start_risk_confirmation(
        {"action": "pause", "sub_action": "void_pause", "reason": "void"},
        behavioral_confidence=0.1,
        is_baselined=False,
        primary_driver="phi_cold_start",
        primary_eisv_source="ode_fallback",
        process_cycle=1,
        monitor_lineage="lineage-a",
        lineage_status="identity_genesis",
        previous_evaluation=None,
        history_gap=False,
        independent_override=None,
        shadow_enabled=True,
        actuation_enabled=False,
    )
    disabled = _evaluate(shadow_enabled=False)

    assert non_risk["policy_candidate"] is False
    assert non_risk["ineligibility_reason"] == "policy_not_risk_pause"
    assert disabled["outcome"] == "disabled"
    assert disabled["would_defer"] is False


def test_actuation_flag_remains_fail_closed_without_durable_state():
    gate = _evaluate(actuation_enabled=True)

    assert gate["actuation_enabled"] is True
    assert gate["actuation_scope"] == "fallback_risk_pause_deferral"
    assert gate["actuation_ready"] is False
    assert gate["actuation_applied"] is False
    assert gate["actuation_blocker"] == "durable_confirmation_state_not_implemented"


def test_non_authored_phi_cold_start_pause_becomes_advisory_guidance():
    guarded = apply_non_authored_cold_start_guard(
        _decision_with_gate(),
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "proceed"
    assert guarded["sub_action"] == "guide"
    assert guarded["original_action"] == "pause"
    assert guarded["original_sub_action"] == "risk_pause"
    assert guarded["cold_start_epistemic_deferred"] is True
    assert guarded["cold_start_epistemic_gate"]["applied"] is True
    assert (
        guarded["cold_start_epistemic_gate"]["enforcement_basis"]
        == NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS
    )


def test_non_authored_phi_cold_start_cirs_risk_block_becomes_guidance():
    guarded = apply_non_authored_cold_start_guard(
        _decision_with_gate(_cirs_block()),
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded["action"] == "proceed"
    assert guarded["sub_action"] == "guide"
    assert guarded["original_action"] == "pause"
    assert guarded["original_sub_action"] == "cirs_block"
    assert guarded["nearest_edge"] == "risk"
    assert guarded["cold_start_epistemic_gate"]["applied"] is True
    original = guarded["cold_start_epistemic_gate"]["original_decision"]
    assert original["action"] == "pause"
    assert original["sub_action"] == "cirs_block"
    assert original["reason"] == "CIRS risk ceiling breached"
    assert original["guidance"] == "Pause to investigate the risk spike."
    assert original["nearest_edge"] == "risk"
    assert original["hard_stop_provenance"]["risk_only"] is True


def test_epistemic_guard_preserves_authoritative_or_uncertain_pauses():
    agent_report = apply_non_authored_cold_start_guard(
        _decision_with_gate(),
        epistemic_class="agent_report",
        enabled=True,
    )
    behavioral_ready = apply_non_authored_cold_start_guard(
        _decision_with_gate(
            behavioral_confidence=0.3,
            primary_driver="behavioral_assessment",
        ),
        epistemic_class="substrate_interpretation",
        enabled=True,
    )
    independent_override = apply_non_authored_cold_start_guard(
        _decision_with_gate(
            primary_driver="independent_verification_floor",
            independent_override="independent_verification_floor",
        ),
        epistemic_class="substrate_observation",
        enabled=True,
    )
    unknown = apply_non_authored_cold_start_guard(
        _decision_with_gate(),
        epistemic_class=None,
        enabled=True,
    )
    disabled = apply_non_authored_cold_start_guard(
        _decision_with_gate(),
        epistemic_class="substrate_interpretation",
        enabled=False,
    )

    for decision in (
        agent_report,
        behavioral_ready,
        independent_override,
        unknown,
        disabled,
    ):
        assert decision["action"] == "pause"
        assert decision["sub_action"] == "risk_pause"
        assert decision["cold_start_epistemic_gate"]["applied"] is False
    assert agent_report["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "agent_authored_report"
    )
    assert independent_override["cold_start_epistemic_gate"][
        "ineligibility_reason"
    ] == "independent_override"
    assert unknown["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "epistemic_class_missing_or_unknown"
    )
    assert disabled["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "guard_disabled"
    )


def test_cirs_risk_guard_preserves_agent_authored_and_behaviorally_ready_pauses():
    agent_report = apply_non_authored_cold_start_guard(
        _decision_with_gate(_cirs_block()),
        epistemic_class="agent_report",
        enabled=True,
    )
    behavioral_ready = apply_non_authored_cold_start_guard(
        _decision_with_gate(
            _cirs_block(),
            behavioral_confidence=0.3,
            primary_driver="behavioral_assessment",
        ),
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    for decision in (agent_report, behavioral_ready):
        assert decision["action"] == "pause"
        assert decision["sub_action"] == "cirs_block"
        assert decision["nearest_edge"] == "risk"
        assert decision["cold_start_epistemic_gate"]["applied"] is False
    assert agent_report["cold_start_epistemic_gate"]["ineligibility_reason"] == (
        "agent_authored_report"
    )
    assert behavioral_ready["cold_start_epistemic_gate"][
        "ineligibility_reason"
    ] == "verdict_source_not_phi_cold_start"


def test_epistemic_guard_never_changes_non_risk_pause():
    decision = {
        "action": "pause",
        "sub_action": "void_pause",
        "reason": "void active",
        "cold_start_confirmation": _evaluate(),
    }

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded == decision


@pytest.mark.parametrize(
    "decision",
    [
        _cirs_block("oscillation"),
        _cirs_block("coherence"),
        _cirs_block(None),
        _cirs_block("unclassified"),
        {
            "action": "pause",
            "sub_action": "system_maintenance",
            "reason": "maintenance window",
        },
    ],
)
def test_epistemic_guard_never_changes_other_pause_paths(decision):
    decision = _decision_with_gate(decision)

    guarded = apply_non_authored_cold_start_guard(
        decision,
        epistemic_class="substrate_interpretation",
        enabled=True,
    )

    assert guarded == decision


def test_reviewed_recovery_recognizes_only_exact_persisted_legacy_trap():
    eligible = evaluate_non_authored_cold_start_trap(
        _legacy_trap_record(),
        enabled=True,
    )
    agent_authored = evaluate_non_authored_cold_start_trap(
        _legacy_trap_record(epistemic_class="agent_report"),
        enabled=True,
    )
    wrong_basis = evaluate_non_authored_cold_start_trap(
        _legacy_trap_record(enforcement_basis="risk_policy"),
        enabled=True,
    )
    missing = evaluate_non_authored_cold_start_trap(None, enabled=True)

    assert eligible["eligible"] is True
    assert eligible["failed_requirements"] == []
    assert eligible["recovery_basis"] == NON_AUTHORED_COLD_START_RECOVERY_BASIS
    assert eligible["observed_enforcement"]["circuit_breaker_applied"] is True
    assert eligible["observed_enforcement"]["actuation_id"] == "actuation-123"
    assert agent_authored["eligible"] is False
    assert "epistemic_class_non_authoring" in agent_authored["failed_requirements"]
    assert wrong_basis["eligible"] is False
    assert "circuit_breaker_applied" not in wrong_basis["failed_requirements"]
    assert "legacy_enforcement_basis_exact" in wrong_basis["failed_requirements"]
    assert wrong_basis["observed_enforcement"]["circuit_breaker_applied"] is True
    assert missing["eligible"] is False
    assert "state_record_present" in missing["failed_requirements"]


def test_reviewed_recovery_does_not_reconstruct_missing_historical_inputs():
    record = _legacy_trap_record()
    policy = record.state_json["eisv_telemetry"]["policy_evaluation"]
    policy.pop("hard_stop_provenance")
    policy["maturity_gate"].pop("hard_stop_provenance")

    observed = evaluate_non_authored_cold_start_trap(record, enabled=True)

    assert observed["eligible"] is False
    assert "risk_only_hard_stop_provenance_exact" in observed[
        "failed_requirements"
    ]
    assert observed["observed_enforcement"]["circuit_breaker_applied"] is True


def test_reviewed_recovery_does_not_reinterpret_historical_cirs_risk_blocks():
    record = _legacy_trap_record(enforcement_basis="non_cold_start_policy")
    policy = record.state_json["eisv_telemetry"]["policy_evaluation"]
    policy["sub_action"] = "cirs_block"
    policy["inputs"]["nearest_edge"] = "risk"
    policy["maturity_gate"] = {
        **_evaluate(decision=_cirs_block()),
        "outcome": "ineligible",
        "eligible": False,
        "would_defer": False,
        "policy_candidate": False,
    }

    observed = evaluate_non_authored_cold_start_trap(record, enabled=True)

    assert observed["eligible"] is False
    assert "policy_was_risk_pause" in observed["failed_requirements"]
    assert "maturity_gate_exact" in observed["failed_requirements"]
    assert "legacy_enforcement_basis_exact" in observed["failed_requirements"]
    assert observed["observed_enforcement"]["circuit_breaker_applied"] is True


def test_recovery_reports_confirmed_agent_report_actuation_without_granting_exception():
    first = _evaluate()
    confirmed = _evaluate(previous=first, cycle=2, behavioral_confidence=0.2)
    observed = evaluate_non_authored_cold_start_trap(
        _legacy_trap_record(
            epistemic_class="agent_report",
            enforcement_basis="phi_cold_start_confirmed",
            maturity_gate=confirmed,
        ),
        enabled=True,
    )

    assert observed["eligible"] is False
    assert observed["observed_enforcement"] == {
        "requested": True,
        "applied": True,
        "mode": "circuit_breaker",
        "basis": "phi_cold_start_confirmed",
        "scope": "runtime_circuit_breaker",
        "actor": "agent_loop_detection",
        "effect": "agent_metadata.status=paused",
        "actuation_id": "actuation-123",
        "applied_at": "2026-08-11T23:47:55+00:00",
        "circuit_breaker_applied": True,
    }
    assert observed["requirements"]["circuit_breaker_applied"] is True
    assert "circuit_breaker_applied" not in observed["failed_requirements"]
    assert "legacy_enforcement_basis_exact" in observed["failed_requirements"]
    assert "applied circuit breaker" in observed["note"]


def test_verdict_driver_names_authority_boundary():
    cold = classify_verdict_driver(
        behavioral_confidence=0.2,
        behavioral_verdict="safe",
        behavioral_enabled=True,
        phi_telemetry=True,
    )
    warm = classify_verdict_driver(
        behavioral_confidence=0.3,
        behavioral_verdict="safe",
        behavioral_enabled=True,
        phi_telemetry=True,
    )
    floored = classify_verdict_driver(
        behavioral_confidence=0.3,
        behavioral_verdict="safe",
        behavioral_enabled=True,
        phi_telemetry=False,
    )

    assert cold == "phi_cold_start"
    assert warm == "behavioral_assessment"
    assert floored == "phi_floor"


def test_monitor_path_surfaces_shadow_without_mutating_pause():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("cold-start-shadow-integration", load_state=False)
    monitor._cold_start_confirmation_lineage_status = "identity_genesis"
    agent_state = {
        "parameters": [0.1, 0.2],
        "ethical_drift": [0.0, 0.0, 0.0],
        "response_text": "Implement a bounded telemetry change.",
        "complexity": 0.4,
        "task_type": "mixed",
    }

    with (
        patch.object(
            monitor,
            "make_decision",
            return_value=_risk_pause(),
        ),
        patch("src.governance_monitor.audit_logger._write_entry"),
    ):
        first = monitor.process_update(agent_state, confidence=0.5)
        second = monitor.process_update(agent_state, confidence=0.5)

    first_gate = first["policy_evaluation"]["maturity_gate"]
    second_gate = second["policy_evaluation"]["maturity_gate"]
    assert first["decision"]["action"] == "pause"
    assert first["enforcement"]["requested"] is True
    assert first["enforcement"]["applied"] is False
    assert first_gate["outcome"] == "shadow_would_defer"
    assert second_gate["outcome"] == "shadow_confirmed"
    assert second_gate["confirmation_count"] == 2


def test_monitor_path_defers_non_authored_phi_cold_start_from_runtime_actuator():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("cold-start-epistemic-guard", load_state=False)
    monitor._cold_start_confirmation_lineage_status = "identity_genesis"
    agent_state = {
        "parameters": [0.1, 0.2],
        "ethical_drift": [0.0, 0.0, 0.0],
        "response_text": "Automatic stop-hook substrate interpretation.",
        "complexity": 0.4,
        "task_type": "mixed",
        "epistemic_class": "substrate_interpretation",
    }

    with (
        patch.object(
            monitor,
            "make_decision",
            return_value=_risk_pause(),
        ),
        patch("src.governance_monitor.audit_logger._write_entry"),
    ):
        result = monitor.process_update(agent_state, confidence=0.5)

    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["sub_action"] == "guide"
    assert result["policy_evaluation"]["suppression"] == {
        "original_action": "pause",
        "original_sub_action": "risk_pause",
        "cold_start_epistemic_deferred": True,
    }
    assert result["policy_evaluation"]["epistemic_gate"]["applied"] is True
    assert result["enforcement"]["requested"] is False
    assert result["enforcement"]["applied"] is False
    assert (
        result["enforcement"]["basis"]
        == NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS
    )


def test_monitor_path_defers_non_authored_cold_start_cirs_risk_block():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("cold-start-cirs-risk-guard", load_state=False)
    monitor._cold_start_confirmation_lineage_status = "identity_genesis"
    agent_state = {
        "parameters": [0.1, 0.2],
        "ethical_drift": [0.0, 0.0, 0.0],
        "response_text": "Automatic stop-hook substrate interpretation.",
        "complexity": 0.4,
        "task_type": "mixed",
        "epistemic_class": "substrate_interpretation",
    }

    with (
        patch.object(
            monitor,
            "make_decision",
            return_value=_cirs_block(),
        ),
        patch("src.governance_monitor.audit_logger._write_entry"),
    ):
        result = monitor.process_update(agent_state, confidence=0.5)

    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["sub_action"] == "guide"
    assert result["decision"]["nearest_edge"] == "risk"
    assert result["policy_evaluation"]["suppression"] == {
        "original_action": "pause",
        "original_sub_action": "cirs_block",
        "cold_start_epistemic_deferred": True,
    }
    maturity_gate = result["policy_evaluation"]["maturity_gate"]
    assert maturity_gate["policy_candidate"] is True
    assert maturity_gate["original_decision"]["nearest_edge"] == "risk"
    epistemic_gate = result["policy_evaluation"]["epistemic_gate"]
    assert epistemic_gate["applied"] is True
    assert epistemic_gate["original_decision"]["nearest_edge"] == "risk"
    assert result["enforcement"]["requested"] is False
    assert result["enforcement"]["applied"] is False
    assert (
        result["enforcement"]["basis"]
        == NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS
    )
