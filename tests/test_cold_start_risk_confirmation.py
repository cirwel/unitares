"""Regression contract for cold-start risk confirmation shadow telemetry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.cold_start_risk_confirmation import (
    NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS,
    NON_AUTHORED_COLD_START_RECOVERY_BASIS,
    apply_non_authored_cold_start_guard,
    classify_verdict_driver,
    evaluate_cold_start_risk_confirmation,
    evaluate_non_authored_cold_start_trap,
)


def _risk_pause():
    return {
        "action": "pause",
        "sub_action": "risk_pause",
        "reason": "UNITARES high-risk verdict",
    }


def _evaluate(*, previous=None, cycle=1, **overrides):
    arguments = {
        "behavioral_confidence": 0.1,
        "is_baselined": False,
        "primary_driver": "phi_cold_start",
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
    return evaluate_cold_start_risk_confirmation(_risk_pause(), **arguments)


def _decision_with_gate(**gate_overrides):
    decision = _risk_pause()
    decision["cold_start_confirmation"] = _evaluate(**gate_overrides)
    return decision


def _legacy_trap_record(
    *,
    epistemic_class="substrate_interpretation",
    enforcement_basis="phi_cold_start_unconfirmed_shadow",
):
    maturity_gate = _evaluate()
    return SimpleNamespace(
        epistemic_class=epistemic_class,
        state_json={
            "epistemic_class": epistemic_class,
            "eisv_telemetry": {
                "schema": "eisv.telemetry.v1",
                "policy_evaluation": {
                    "action": "pause",
                    "sub_action": "risk_pause",
                    "inputs": {
                        "verdict_source": "phi_cold_start",
                        "primary_eisv_source": "ode_fallback",
                    },
                    "maturity_gate": maturity_gate,
                },
                "enforcement": {
                    "requested": True,
                    "applied": True,
                    "mode": "circuit_breaker",
                    "basis": enforcement_basis,
                    "actor": "agent_loop_detection",
                    "effect": "agent_metadata.status=paused",
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
    assert gate["actuation_applied"] is False
    assert gate["original_decision"] == {
        "action": "pause",
        "sub_action": "risk_pause",
        "reason": "UNITARES high-risk verdict",
    }


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
    assert agent_authored["eligible"] is False
    assert "epistemic_class_non_authoring" in agent_authored["failed_requirements"]
    assert wrong_basis["eligible"] is False
    assert "circuit_breaker_applied" in wrong_basis["failed_requirements"]
    assert missing["eligible"] is False
    assert "state_record_present" in missing["failed_requirements"]


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
