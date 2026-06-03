"""
Pattern Analysis for Cross-Monitoring

Provides analysis functions for detecting trends, anomalies, and patterns
in agent governance history. Optimized for AI agent consumption.
"""

from typing import Dict, List, Optional
import numpy as np
from collections import Counter


def analyze_trend(values: List[float], window: int = 5) -> str:
    """
    Analyze trend in a time series.
    
    Returns: "increasing", "decreasing", or "stable"
    """
    if len(values) < 2:
        return "stable"
    
    if len(values) < window:
        window = len(values)
    
    recent = values[-window:]
    older = values[-window*2:-window] if len(values) >= window*2 else values[:-window]
    
    if len(older) == 0:
        return "stable"
    
    recent_mean = np.mean(recent)
    older_mean = np.mean(older)
    
    change = recent_mean - older_mean
    threshold = 0.05  # 5% change threshold
    
    if abs(change) < threshold:
        return "stable"
    elif change > 0:
        return "increasing"
    else:
        return "decreasing"


def detect_anomalies_in_history(
    risk_history: List[float],
    coherence_history: List[float],
    timestamps: List[str]
) -> List[Dict]:
    """
    Detect anomalies in agent history.
    
    Returns list of anomaly dicts with type, severity, timestamp, description.
    """
    anomalies = []
    
    if len(risk_history) < 3:
        return anomalies
    
    # Risk spike detection
    recent_risk = risk_history[-3:]
    older_risk = risk_history[-6:-3] if len(risk_history) >= 6 else risk_history[:-3]
    
    if len(older_risk) > 0:
        recent_mean = np.mean(recent_risk)
        older_mean = np.mean(older_risk)
        change = recent_mean - older_mean
        
        if change > 0.15:  # 15% increase
            severity = "high" if change > 0.25 else "medium"
            anomalies.append({
                "type": "risk_spike",
                "severity": severity,
                "timestamp": timestamps[-1] if timestamps else None,
                "description": f"Risk increased from {older_mean:.2f} to {recent_mean:.2f} ({change:.2f} change)",
                "context": {
                    "previous_risk": float(older_mean),
                    "current_risk": float(recent_mean),
                    "change": float(change)
                }
            })
    
    # Coherence drop detection
    if len(coherence_history) >= 5:
        recent_coherence = coherence_history[-3:]
        older_coherence = coherence_history[-5:-3]
        
        if len(older_coherence) > 0:
            recent_mean = np.mean(recent_coherence)
            older_mean = np.mean(older_coherence)
            change = older_mean - recent_mean  # Negative change = drop
            
            if change > 0.05:  # 5% drop
                severity = "high" if change > 0.10 else "medium"
                anomalies.append({
                    "type": "coherence_drop",
                    "severity": severity,
                    "timestamp": timestamps[-1] if timestamps else None,
                    "description": f"Coherence dropped from {older_mean:.2f} to {recent_mean:.2f} ({change:.2f} change)",
                    "context": {
                        "previous_coherence": float(older_mean),
                        "current_coherence": float(recent_mean),
                        "change": float(-change)
                    }
                })
    
    return anomalies


def _get_behavioral_histories(monitor) -> Optional[Dict[str, list]]:
    """Get EISV histories from behavioral state when confident, else None."""
    try:
        beh = monitor._behavioral_state
        if beh.confidence < 0.3:
            return None
        return {
            "E": list(beh.E_history),
            "I": list(beh.I_history),
            "S": list(beh.S_history),
            "V": list(beh.V_history),
        }
    except (AttributeError, TypeError):
        return None


def build_decision_distribution(decision_history) -> Dict[str, int]:
    """Collapse a raw decision vocabulary into the observe summary surface.

    Single source of truth for the proceed/pause mapping, shared by the
    in-memory monitor path (analyze_agent_patterns) and the Postgres-truth
    override in handle_observe_agent. `decision_history` is any iterable of
    action strings ('proceed'|'pause'|'approve'|'reflect'|'revise'|'reject').
    """
    c = Counter(decision_history)
    return {
        "proceed": c.get("proceed", 0) + c.get("approve", 0) + c.get("reflect", 0) + c.get("revise", 0),
        "pause": c.get("pause", 0) + c.get("reject", 0),
        # Backward compatibility
        "approve": c.get("approve", 0),
        "reflect": c.get("reflect", 0) + c.get("revise", 0),
        "reject": c.get("reject", 0),
    }


def build_verdict_distribution(verdict_history) -> Dict[str, int]:
    """Collapse the EISV verdict tier vocabulary (safe/caution/high-risk).

    Shared by the monitor path and the Postgres-truth override; `verdict_history`
    is any iterable of verdict tier strings.
    """
    c = Counter(verdict_history)
    return {
        "safe": c.get("safe", 0),
        "caution": c.get("caution", 0),
        "high-risk": c.get("high-risk", 0),
        "total": sum(c.values()),
    }


def analyze_agent_patterns(
    monitor,
    include_history: bool = True
) -> Dict:
    """
    Analyze patterns in an agent's governance history.

    Returns structured analysis optimized for AI consumption.
    """
    state = monitor.state

    # Current state — behavioral-first EISV
    try:
        pE, pI, pS, pV = monitor.get_primary_eisv()
    except (AttributeError, TypeError, ValueError):
        pE, pI, pS, pV = float(state.E), float(state.I), float(state.S), float(state.V)
    risk_score = float(state.risk_history[-1]) if state.risk_history else 0.0
    current_state = {
        "E": pE,
        "I": pI,
        "S": pS,
        "V": pV,
        "coherence": float(state.coherence),
        "risk_score": risk_score,  # Governance/operational risk
        "lambda1": float(state.lambda1),
        "update_count": state.update_count
    }

    # EISV histories: behavioral-first, ODE fallback
    beh_hist = _get_behavioral_histories(monitor)
    e_hist = beh_hist["E"] if beh_hist else state.E_history
    i_hist = beh_hist["I"] if beh_hist else state.I_history
    s_hist = beh_hist["S"] if beh_hist else state.S_history
    v_hist = beh_hist["V"] if beh_hist else state.V_history

    # Pattern analysis
    patterns = {}

    if len(state.risk_history) >= 2:
        patterns["risk_trend"] = analyze_trend(state.risk_history)
    else:
        patterns["risk_trend"] = "stable"

    if len(state.coherence_history) >= 2:
        patterns["coherence_trend"] = analyze_trend(state.coherence_history)
    else:
        patterns["coherence_trend"] = "stable"

    if len(e_hist) >= 2:
        patterns["E_trend"] = analyze_trend(e_hist)
    else:
        patterns["E_trend"] = "stable"

    # Overall trend
    if patterns.get("risk_trend") == "decreasing" and patterns.get("coherence_trend") == "increasing":
        patterns["trend"] = "improving"
    elif patterns.get("risk_trend") == "increasing" and patterns.get("coherence_trend") == "decreasing":
        patterns["trend"] = "degrading"
    else:
        patterns["trend"] = "stable"

    # Anomaly detection
    timestamps = state.timestamp_history if hasattr(state, 'timestamp_history') else []
    anomalies = detect_anomalies_in_history(
        state.risk_history,
        state.coherence_history,
        timestamps
    )

    # Summary statistics
    # verdict_history (safe/caution/high-risk) is a separate vocabulary from
    # decision_history (proceed/pause/...) — both surfaced so DB-hydrated
    # agents whose state_json rows lack the (newer) action key still expose a
    # non-empty governance signal via verdict_distribution.
    decision_history = getattr(state, 'decision_history', [])
    verdict_history = getattr(state, 'verdict_history', [])

    summary = {
        "total_updates": state.update_count,
        "mean_risk": float(np.mean(state.risk_history)) if state.risk_history else 0.0,
        "mean_coherence": float(np.mean(state.coherence_history)) if state.coherence_history else 0.0,
        "decision_distribution": build_decision_distribution(decision_history),
        "verdict_distribution": build_verdict_distribution(verdict_history),
    }

    result = {
        "current_state": current_state,
        "patterns": patterns,
        "anomalies": anomalies,
        "summary": summary
    }

    if include_history and len(state.risk_history) > 0:
        # Include recent history (last 10 updates) — behavioral EISV, ODE risk/coherence
        recent_window = min(10, len(state.risk_history))
        eisv_window = min(10, len(e_hist))
        result["recent_history"] = {
            "timestamps": timestamps[-recent_window:] if timestamps else [],
            "risk_history": [float(r) for r in state.risk_history[-recent_window:]],
            "coherence_history": [float(c) for c in state.coherence_history[-recent_window:]],
            "E_history": [float(e) for e in e_hist[-eisv_window:]],
            "I_history": [float(i) for i in i_hist[-eisv_window:]],
            "S_history": [float(s) for s in s_hist[-eisv_window:]],
            "V_history": [float(v) for v in v_hist[-eisv_window:]]
        }

    return result

