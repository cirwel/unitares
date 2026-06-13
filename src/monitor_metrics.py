"""
Governance monitor metrics and export functions.

Extracted from governance_monitor.py. These functions produce read-only views
of monitor state — metrics snapshots, EISV labels, and history export.

Each function takes the monitor instance as first argument to access
agent_id, state, and config without coupling to the class hierarchy.
"""

import json
import csv
import io
import math
from collections import Counter
from typing import Dict, Any

import time as _time

import numpy as np

from config.governance_config import config
from governance_core.parameters import get_params_profile_name, DEFAULT_WEIGHTS
from governance_core.scoring import phi_objective, verdict_from_phi
from governance_core import approximate_stability_check
from src.health_thresholds import HealthThresholds

# Module-level stability cache (shared across all monitors, keyed by agent_id)
_stability_cache: Dict[str, Dict[str, Any]] = {}
_STABILITY_CACHE_TTL = 300  # 5 minutes
_STABILITY_CACHE_MAX = 256  # max entries before eviction


def get_monitor_metrics(monitor: Any, include_state: bool = True) -> Dict:
    """
    Returns current governance metrics for a monitor instance.

    Args:
        monitor: UNITARESMonitor instance (needs .agent_id, .state, ._last_oscillation_state)
        include_state: If False, excludes the nested 'state' dict to reduce response size.
                      All state values (E, I, S, V, coherence, lambda1) are still included at top level.
                      Default True for backward compatibility.
    """
    state = monitor.state

    # Calculate decision statistics
    decision_counts = {}
    decision_history = getattr(state, 'decision_history', [])
    if decision_history:
        counts = Counter(decision_history)
        decision_counts = dict(counts)
        decision_counts['total'] = len(decision_history)

    # Check stability (Lyapunov eigenvalue analysis, cached 5 min per agent)
    now = _time.monotonic()
    cached = _stability_cache.get(monitor.agent_id)
    if cached and (now - cached["_ts"]) < _STABILITY_CACHE_TTL:
        stability_result = cached
    else:
        stability_result = approximate_stability_check(
            theta=state.unitaires_theta,
            dt=config.DT,
        )
        stability_result["_ts"] = now
        _stability_cache[monitor.agent_id] = stability_result
        # Evict oldest entries if cache exceeds max size
        if len(_stability_cache) > _STABILITY_CACHE_MAX:
            oldest_key = min(_stability_cache, key=lambda k: _stability_cache[k].get("_ts", 0))
            del _stability_cache[oldest_key]

    # Calculate status consistently with process_update()
    # Health status uses RECENT TREND (mean of last 10 risk scores), not overall mean
    if len(state.risk_history) >= 10:
        current_risk = float(np.mean(state.risk_history[-10:]))
    elif state.risk_history:
        current_risk = float(np.mean(state.risk_history))
    else:
        current_risk = None

    # Use LATEST (point-in-time) risk_score for consistency with process_update
    latest_risk_score = float(state.risk_history[-1]) if state.risk_history else None

    # Smoothed trend (for historical context)
    smoothed_risk_score = current_risk

    # Overall mean risk (for display/comparison)
    mean_risk = float(np.mean(state.risk_history)) if state.risk_history else 0.0

    # Status calculation - USE LATEST VALUE to match process_update behavior
    health_checker = HealthThresholds()
    status_risk = latest_risk_score if latest_risk_score is not None else current_risk

    health_status_obj, _ = health_checker.get_health_status(
        risk_score=status_risk,
        coherence=state.coherence,
        void_active=state.void_active
    )
    status = health_status_obj.value

    # Compute Phi and verdict from current state
    phi = phi_objective(
        state=state.unitaires_state,
        delta_eta=[0.0, 0.0, 0.0],
        weights=DEFAULT_WEIGHTS
    )
    ode_verdict = verdict_from_phi(phi)

    # Prefer behavioral verdict when available (observation-first, not thermostat)
    behavioral_verdict = getattr(monitor, '_last_behavioral_verdict', None)
    verdict = behavioral_verdict if behavioral_verdict else ode_verdict

    risk_score_value = current_risk if current_risk is not None else mean_risk

    # Get regime with fallback for backward compatibility
    regime = getattr(state, 'regime', 'divergence')

    # Honest initialization: return None for computed metrics when no updates yet
    is_uninitialized = state.update_count == 0

    # Primary EISV: behavioral when confident, ODE fallback
    beh = getattr(monitor, '_behavioral_state', None)
    if beh is not None and beh.confidence >= 0.3:
        pE, pI, pS, pV = float(beh.E), float(beh.I), float(beh.S), float(beh.V)
    else:
        pE, pI, pS, pV = float(state.E), float(state.I), float(state.S), float(state.V)

    result = {
        'agent_id': monitor.agent_id,
        'E': pE,
        'I': pI,
        'S': pS,
        'V': pV,
        'coherence': None if is_uninitialized else float(state.coherence),
        'lambda1': float(state.lambda1),
        'regime': str(regime),
        'status': 'uninitialized' if is_uninitialized else status,
        'initialized': not is_uninitialized,
        'history_size': len(state.V_history),
        'current_risk': None if is_uninitialized else current_risk,
        'mean_risk': None if is_uninitialized else mean_risk,
        'risk_score': None if is_uninitialized else risk_score_value,
        'latest_risk_score': None if is_uninitialized else latest_risk_score,
        'phi': float(phi),
        'verdict': 'uninitialized' if is_uninitialized else verdict,
        'void_active': bool(state.void_active),
        'void_frequency': float(np.mean([float(abs(v) > config.VOID_THRESHOLD_INITIAL)
                                        for v in state.V_history])) if state.V_history else 0.0,
        'decision_statistics': decision_counts,
        'stability': {
            'stable': stability_result['stable'],
            'alpha_estimate': stability_result['alpha_estimate'],
            'violations': stability_result['violations'],
            'notes': stability_result['notes']
        },
        'ode': {
            'E': float(state.E),
            'I': float(state.I),
            'S': float(state.S),
            'V': float(state.V),
        }
    }

    # Basin classification — unified across all profiles via classify_basin()
    from config.governance_config import classify_basin

    profile = get_params_profile_name()
    I = pI  # Use behavioral-preferred I for basin analysis

    # Use risk from the metrics we just built, falling back to history
    _risk_for_basin = result.get("risk_score")
    if _risk_for_basin is None:
        _risk_for_basin = state.risk_history[-1] if state.risk_history else 0.0
    basin = classify_basin(E=pE, I=I, S=pS, V=state.V,
                           coherence=state.coherence, risk_score=_risk_for_basin)

    basin_warning = None
    if basin == "low":
        basin_warning = "LOW basin: one or more critical dimensions breached — elevated risk"
    elif basin == "boundary":
        basin_warning = "Near basin boundary: small shocks can shift operating regime"

    S = pS  # Use behavioral-preferred S for convergence tracking
    if profile == "v41":
        I_target = 0.91
        S_target = 0.001
        E_target = 0.91
    else:
        I_target = 1.0
        S_target = 0.0
        E_target = 0.7

    eq_dist = float(((I_target - I) ** 2 + (S - S_target) ** 2) ** 0.5)

    dt = float(getattr(config, "DT", 0.1))
    alpha = 0.1
    contraction = max(1e-6, 1.0 - alpha * dt)
    eps = 0.02
    est_updates = None
    if eq_dist > 0 and contraction < 1.0:
        try:
            est_updates = int(math.ceil(max(0.0, math.log(eps / eq_dist) / math.log(contraction))))
        except Exception:
            est_updates = None

    result["unitares_v41"] = {
        "params_profile": profile,
        "basin": basin,
        "basin_warning": basin_warning,
        "equilibrium": {
            # Prescribed design attractor (profile-dependent) the controller
            # converges toward — distinct from the instantaneous linear
            # fixed-point in saturation_diagnostics.I_equilibrium, which is
            # derived from current params/state (dogfood 2026-06-13: the two
            # were surfaced side by side, 1.0 vs 0.52, with no label).
            "kind": "design_attractor_target",
            "I_target": I_target,
            "S_target": S_target,
            "E_target": E_target,
        },
        "convergence": {
            "equilibrium_distance": eq_dist,
            "estimated_updates_to_eps": est_updates,
            "eps": eps,
            "note": "Heuristic estimate (assumes contraction rate alpha~0.1 and dt=config.DT).",
        },
    }

    # Include nested state dict only if requested
    if include_state:
        result['state'] = state.to_dict()

    # HCK v3.0 / CIRS v0.1: Reflexive control and resonance metrics
    result['hck'] = {
        'rho': float(getattr(state, 'current_rho', 0.0)),
        'CE': float(state.CE_history[-1]) if hasattr(state, 'CE_history') and state.CE_history else 0.0,
        'rho_history_len': len(getattr(state, 'rho_history', [])),
        'CE_history_len': len(getattr(state, 'CE_history', []))
    }

    last_osc = getattr(monitor, '_last_oscillation_state', None)
    result['cirs'] = {
        'oi': float(last_osc.oi) if last_osc else 0.0,
        'flips': int(last_osc.flips) if last_osc else 0,
        'resonant': bool(last_osc.resonant) if last_osc else False,
        'trigger': last_osc.trigger if last_osc else None,
        'resonance_events': int(getattr(state, 'resonance_events', 0)),
        'damping_applied_count': int(getattr(state, 'damping_applied_count', 0)),
        'oi_history_len': len(getattr(state, 'oi_history', []))
    }

    return result


def get_eisv_labels() -> Dict:
    """Returns EISV metric labels and descriptions for API documentation.

    EISV = the four core UNITARES state variables:
    - E: Energy or presence
    - I: Information integrity
    - S: Entropy
    - V: Void integral
    """
    return {
        'E': {
            'label': 'Energy',
            'description': 'Energy or presence',
            'user_friendly': 'How engaged and energized your work feels',
            'range': '[0.0, 1.0]'
        },
        'I': {
            'label': 'Information Integrity',
            'description': 'Information integrity',
            'user_friendly': 'Consistency and coherence of your approach',
            'range': '[0.0, 1.0]'
        },
        'S': {
            'label': 'Entropy',
            'description': 'Entropy (disorder/uncertainty)',
            'user_friendly': 'How scattered or fragmented things are',
            'range': '[0.0, 1.0]'
        },
        'V': {
            'label': 'Void Integral',
            'description': 'Void integral (E-I imbalance accumulation)',
            'user_friendly': 'Accumulated strain from energy-integrity mismatch',
            'range': '[-1.0, 1.0]'
        }
    }


def export_monitor_history(monitor: Any, format: str = 'json') -> str:
    """Exports complete history for analysis.

    Args:
        monitor: UNITARESMonitor instance (needs .agent_id, .state)
        format: 'json' or 'csv'
    """
    state = monitor.state

    # Backward compatibility: ensure decision_history and lambda1_history exist
    decision_history = getattr(state, 'decision_history', [])
    lambda1_history = getattr(state, 'lambda1_history', [])

    history = {
        'agent_id': monitor.agent_id,
        'timestamps': state.timestamp_history,
        'E_history': state.E_history,
        'I_history': state.I_history,
        'S_history': state.S_history,
        'V_history': state.V_history,
        'coherence_history': state.coherence_history,
        'risk_history': state.risk_history,
        'attention_history': state.risk_history,  # Legacy alias — risk_history is the primary field
        'decision_history': decision_history,
        'lambda1_history': lambda1_history,
        'lambda1_final': state.lambda1,
        'total_updates': state.update_count,
        'total_time': state.time
    }

    if format == 'json':
        return json.dumps(history, indent=2)
    elif format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(['update', 'timestamp', 'E', 'I', 'S', 'V', 'coherence', 'risk_score', 'decision', 'lambda1'])

        num_rows = len(state.V_history)
        for i in range(num_rows):
            row = [
                i + 1,
                state.timestamp_history[i] if i < len(state.timestamp_history) else '',
                state.E_history[i] if i < len(state.E_history) else '',
                state.I_history[i] if i < len(state.I_history) else '',
                state.S_history[i] if i < len(state.S_history) else '',
                state.V_history[i] if i < len(state.V_history) else '',
                state.coherence_history[i] if i < len(state.coherence_history) else '',
                state.risk_history[i] if i < len(state.risk_history) else '',
                decision_history[i] if i < len(decision_history) else '',
                lambda1_history[i] if i < len(lambda1_history) else ''
            ]
            writer.writerow(row)

        # Summary row
        writer.writerow([])
        writer.writerow(['Summary', '', '', '', '', '', '', '', ''])
        writer.writerow(['agent_id', monitor.agent_id, '', '', '', '', '', '', ''])
        writer.writerow(['total_updates', state.update_count, '', '', '', '', '', '', ''])
        writer.writerow(['total_time', state.time, '', '', '', '', '', '', ''])
        writer.writerow(['lambda1_final', state.lambda1, '', '', '', '', '', '', ''])

        return output.getvalue()
    else:
        raise ValueError(f"Unsupported format: {format}")
