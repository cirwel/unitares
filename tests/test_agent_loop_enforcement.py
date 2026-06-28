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

    mark_circuit_breaker_enforcement_applied(
        result,
        actor="agent_loop_detection",
        effect="agent_metadata.status=paused",
    )

    assert result["enforcement"] == {
        "requested": True,
        "applied": True,
        "mode": "circuit_breaker",
        "actor": "agent_loop_detection",
        "effect": "agent_metadata.status=paused",
        "note": "Circuit breaker applied at the runtime boundary after policy evaluation.",
    }
    assert result["paused"] is True
    assert result["circuit_breaker_triggered"] is True
