"""Tests for the governance pause actuator metadata in agent loop handling."""

from __future__ import annotations


def test_mark_circuit_breaker_enforcement_applied_preserves_policy_request():
    """The actuator should be explicit and separate from policy evaluation."""
    from src.agent_loop_detection import mark_circuit_breaker_enforcement_applied

    result = {
        "decision": {"action": "pause", "reason": "Low basin"},
        "enforcement": {
            "requested": True,
            "applied": False,
            "mode": "circuit_breaker_candidate",
            "basis": "phi_cold_start_fail_closed",
            "maturity_gate": {
                "outcome": "ineligible",
                "actuation_applied": False,
            },
            "actor": None,
            "effect": None,
            "note": (
                "Policy requested enforcement. This envelope is the pre-actuation "
                "candidate; the authenticated update boundary applies it as a circuit "
                "breaker (agent metadata -> status=paused, blocking later writes) and "
                "overwrites this with applied=true. A non-actuating path (e.g. "
                "simulate) leaves it unapplied."
            ),
        },
    }

    returned_id = mark_circuit_breaker_enforcement_applied(
        result,
        actor="agent_loop_detection",
        effect="agent_metadata.status=paused",
        actuation_id="actuation-123",
        applied_at="2026-08-11T23:47:55+00:00",
    )

    assert result["enforcement"] == {
        "schema": "governance.enforcement.v1",
        "scope": "runtime_circuit_breaker",
        "requested": True,
        "applied": True,
        "mode": "circuit_breaker",
        "basis": "phi_cold_start_fail_closed",
        "maturity_gate": {
            "outcome": "ineligible",
            "actuation_applied": False,
        },
        "actor": "agent_loop_detection",
        "effect": "agent_metadata.status=paused",
        "actuation_id": "actuation-123",
        "applied_at": "2026-08-11T23:47:55+00:00",
        "note": "Circuit breaker applied at the runtime boundary after policy evaluation.",
    }
    assert returned_id == "actuation-123"
    assert result["paused"] is True
    assert result["circuit_breaker_triggered"] is True
