"""Governance vocabulary glossary — embedded at point-of-use.

The dogfood feedback (KG note 2026-05-08T19:05:51, issue #428) flagged that
agents meet UNITARES vocabulary cold and have no way to learn what verdicts,
basins, modes, trajectories, or drift components mean without reading the
documentation. The architect's recommendation: extend the existing
metrics-block range/ideal pattern to every value at point-of-use, not in a
separate glossary doc.

This module is the source of truth for that vocabulary. Helpers wrap a bare
value with `meaning` (and where applicable `next_action`, `range`, `ideal`)
so the value-at-the-call-site self-describes.

Pattern:

    >>> explain_verdict("pause")
    {
      "value": "pause",
      "meaning": "Needs attention.",
      "next_action": "Stop current work, reflect, ..."
    }

The wrapper preserves the original value at "value" so existing consumers
that read `payload["verdict"] == "pause"` keep working when they read
`payload["verdict"]["value"]` instead — but to preserve compatibility the
default convention is: emit BOTH the bare value and a peer "verdict_meta"
field with the explanation. Callers can adopt whichever shape suits them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# -----------------------------------------------------------------------------
# VERDICTS — issued by governance after each check-in
# -----------------------------------------------------------------------------

VERDICTS: Dict[str, Dict[str, str]] = {
    "proceed": {
        "meaning": "State is healthy.",
        "next_action": "Continue working normally.",
    },
    "guide": {
        "meaning": "Something is slightly off.",
        "next_action": "Read the guidance text and adjust approach.",
    },
    "pause": {
        "meaning": "Needs attention.",
        "next_action": "Stop current work, reflect, consider dialectic review.",
    },
    "reject": {
        "meaning": "Significant concern.",
        "next_action": "Requires dialectic review or human input.",
    },
    "uninitialized": {
        "meaning": "Agent has no recorded state yet.",
        "next_action": "Submit one process_agent_update to activate governance.",
    },
    "unbound": {
        "meaning": "No identity bound to this session.",
        "next_action": "Call onboard() to mint an identity.",
    },
    "continue": {
        "meaning": "Synonym of proceed; legacy label still used in some payloads.",
        "next_action": "Continue working normally.",
    },
}


# -----------------------------------------------------------------------------
# BASINS — region of EISV state space
# -----------------------------------------------------------------------------

BASINS: Dict[str, Dict[str, str]] = {
    "high": {
        "meaning": "Healthy. E and I are high; S and V are low. Normal operating range.",
    },
    "low": {
        "meaning": "Degraded. May need recovery or intervention.",
    },
    "boundary": {
        "meaning": "Transitioning between basins. Verdicts may carry margin: tight.",
    },
    "critical": {
        "meaning": "Circuit breaker imminent. Pause and reassess.",
    },
}


# -----------------------------------------------------------------------------
# MODES — 8-pattern map of (high_E, high_I, high_S)
# -----------------------------------------------------------------------------
# Source: src/governance_state.py:_interpret_mode patterns table.

MODES: Dict[str, Dict[str, str]] = {
    "collaborating":       {"meaning": "high E, high I, high S — productive social engagement"},
    "building_alone":      {"meaning": "high E, high I, low S  — focused independent work"},
    "exploring_together":  {"meaning": "high E, low I, high S  — open-ended group exploration"},
    "exploring_alone":     {"meaning": "high E, low I, low S   — solo exploration; consider consolidating"},
    "executing_together":  {"meaning": "low E, high I, high S  — coordinated execution"},
    "executing_alone":     {"meaning": "low E, high I, low S   — disciplined solo execution"},
    "drifting_together":   {"meaning": "low E, low I, high S   — high social, low productivity"},
    "stalled":             {"meaning": "low everything — new task or external input needed"},
}


# -----------------------------------------------------------------------------
# TRAJECTORIES — direction of recent state movement
# -----------------------------------------------------------------------------
# Source: src/governance_state.py:_interpret_trajectory.

TRAJECTORIES: Dict[str, Dict[str, str]] = {
    "improving":  {"meaning": "Value trajectory positive (V > 0.1)."},
    "stable":     {"meaning": "Steady; no significant V drift."},
    "declining":  {"meaning": "Value trajectory negative (V < -0.1). Simplify or seek input."},
    "stuck":      {"meaning": "Multiple pauses in recent decisions. Try a different approach or request dialectic."},
}


# -----------------------------------------------------------------------------
# DRIFT_COMPONENTS — concrete ethical-drift dimensions
# -----------------------------------------------------------------------------
# Source: src/monitor_result.py reads dv.calibration_deviation etc.

DRIFT_COMPONENTS: Dict[str, Dict[str, str]] = {
    "calibration_deviation": {
        "meaning": "Stated confidence vs observed outcome mismatch.",
        "range": "[0, 1]",
        "ideal": "<0.1",
    },
    "complexity_divergence": {
        "meaning": "Reported vs estimated task complexity gap.",
        "range": "[0, 1]",
        "ideal": "<0.1",
    },
    "coherence_deviation": {
        "meaning": "Coherence drift from expected baseline.",
        "range": "[0, 1]",
        "ideal": "<0.1",
    },
    "stability_deviation": {
        "meaning": "State stability variance from baseline.",
        "range": "[0, 1]",
        "ideal": "<0.1",
    },
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _wrap(value: Any, table: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap a value with its glossary entry, or fall back to {value, meaning: 'unknown'}."""
    if value is None:
        return {"value": None}
    info = table.get(str(value))
    if info is None:
        return {"value": value, "meaning": f"unknown (not in glossary)"}
    return {"value": value, **info}


def explain_verdict(verdict: Optional[str]) -> Dict[str, Any]:
    """Wrap a verdict value with meaning + next_action."""
    return _wrap(verdict, VERDICTS)


def explain_basin(basin: Optional[str]) -> Dict[str, Any]:
    """Wrap a basin value with meaning."""
    return _wrap(basin, BASINS)


def explain_mode(mode: Optional[str]) -> Dict[str, Any]:
    """Wrap a mode value with meaning."""
    return _wrap(mode, MODES)


def explain_trajectory(trajectory: Optional[str]) -> Dict[str, Any]:
    """Wrap a trajectory value with meaning."""
    return _wrap(trajectory, TRAJECTORIES)


def annotate_drift_components(drift: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    """Decorate each ethical-drift component with meaning, range, ideal.

    Components without a glossary entry pass through with just `value`.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in drift.items():
        info = DRIFT_COMPONENTS.get(key)
        if info is None:
            out[key] = {"value": value}
        else:
            out[key] = {"value": value, **info}
    return out
