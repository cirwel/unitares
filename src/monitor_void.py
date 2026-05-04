"""Void state detection and frequency calculation for governance monitor."""

import numpy as np

from config.governance_config import config


def check_void_state(state, agent_class: str | None = None) -> bool:
    """
    Check if system is in void state: |V| > adaptive (or class-overridden) threshold.

    Updates state.void_active in place.

    Args:
        state: GovernanceState instance with V, V_history, void_active attrs.
        agent_class: Optional agent class name (per
            `src/grounding/class_indicator.py::classify_agent`). When provided
            AND in `GovernanceConfig.VOID_THRESHOLD_BY_CLASS`, returns the
            class-specific override threshold (RFC v0.11 §7.13.6 PR 3 — interim
            safety net). Default `None` preserves prior behavior.

            For convenience, callers may also leave this None and populate
            `state.agent_class` instead — this function reads `state.agent_class`
            via `getattr` as a fallback.

    Returns:
        True if system is in void state.
    """
    V_history = np.array(state.V_history) if state.V_history else np.array([state.V])

    # Resolution order: explicit kwarg → state attribute → None.
    resolved_class = agent_class or getattr(state, "agent_class", None)

    threshold = config.get_void_threshold(V_history, adaptive=True, agent_class=resolved_class)

    void_active = bool(abs(state.V) > threshold)
    state.void_active = void_active

    return void_active


def calculate_void_frequency(state, agent_class: str | None = None) -> float:
    """
    Calculate void frequency from V history.

    Returns fraction of time system was in void state (|V| > threshold).
    Uses adaptive (or class-overridden) threshold for each historical point.

    Args:
        state: GovernanceState instance with V_history attr.
        agent_class: Optional agent class name. Same semantics as in
            `check_void_state` — class override applies if provided and
            registered in VOID_THRESHOLD_BY_CLASS.
    """
    if not state.V_history or len(state.V_history) < 10:
        return 0.0

    window = min(100, len(state.V_history))
    recent_V = np.array(state.V_history[-window:])

    resolved_class = agent_class or getattr(state, "agent_class", None)
    threshold = config.get_void_threshold(recent_V, adaptive=True, agent_class=resolved_class)

    void_count = np.sum(np.abs(recent_V) > threshold)
    return float(void_count) / len(recent_V)
