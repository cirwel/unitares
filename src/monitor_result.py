"""Result assembly for governance monitor process_update."""

from datetime import datetime, timezone
from typing import Dict

from governance_core import get_agent_baseline
from src.drift_telemetry import record_drift
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Eligibility threshold for the complexity-calibration signal. CANONICAL —
# response_formatter (mirror line) and updates/enrichments
# (_get_complexity_disagreement, KG-search gate) import this rather than
# repeating the literal, so the three gates can't drift apart.
DIVERGENCE_LINE_THRESHOLD = 0.15
# The signal is NOVEL only on the first crossing or when the signed gap
# moves by more than this since the last surfaced value.
_DIVERGENCE_NOVELTY_DELTA = 0.10


_POLICY_INPUT_FIELDS = (
    "coherence",
    "risk_score",
    "phi",
    "verdict",
    "void_active",
)


def _build_policy_evaluation(decision: Dict, metrics: Dict) -> Dict:
    """Describe the policy layer that consumed measurements.

    EISV/risk/coherence are instrument readings. ``monitor_decision`` is the
    policy layer that maps those readings to guidance/action. Keeping this
    envelope distinct from ``decision`` lets clients and operators inspect the
    policy without treating the measurement itself as the actuator.
    """
    inputs = {
        "basin": decision.get("basin"),
        **{field: metrics.get(field) for field in _POLICY_INPUT_FIELDS},
        "margin": decision.get("margin"),
        "nearest_edge": decision.get("nearest_edge"),
    }
    return {
        "policy_name": "monitor_decision",
        "policy_version": "v1",
        "action": decision.get("action"),
        "sub_action": decision.get("sub_action"),
        "reason": decision.get("reason"),
        "guidance": decision.get("guidance"),
        "inputs": inputs,
        "measurement_role": "EISV/risk/coherence are policy inputs, not the actuator itself.",
    }


def _build_enforcement_stub(decision: Dict) -> Dict:
    """Describe actuator state before the runtime boundary applies it."""
    requested = decision.get("action") in {"pause", "reject"}
    return {
        "requested": requested,
        "applied": False,
        "mode": "circuit_breaker_candidate" if requested else "advisory",
        "actor": None,
        "effect": None,
        "note": (
            "Policy requested enforcement; actuator state is applied by the caller/runtime boundary."
            if requested else
            "No enforcement requested by policy."
        ),
    }


def _complexity_divergence_novel(monitor, cm) -> bool:
    """True when the complexity divergence is worth surfacing again.

    Novelty gate for the mirror's complexity-calibration line: only the
    first threshold crossing, or a materially changed SIGNED gap
    (magnitude shift > _DIVERGENCE_NOVELTY_DELTA, which includes
    direction flips), counts as novel. A stable session-long gap
    repeating the same line on every check-in is noise, not signal
    (dogfood 2026-06-10). The signed gap (self − derived) is tracked
    rather than |divergence| so an over→under-reporting flip of equal
    magnitude still registers.

    Mutates ``monitor._last_surfaced_complexity_gap`` when returning
    True — deliberate, documented on the attribute
    (governance_monitor.__init__). Not persisted, and per-monitor-
    instance: after a restart — or once per worker under multi-process
    serving, where each worker holds its own monitor cache — the line
    may fire anew (acceptable session-scoped novelty; degrades toward
    the old always-fire behavior, never to wrong output).
    """
    if cm.complexity_divergence <= DIVERGENCE_LINE_THRESHOLD:
        return False
    self_cx = cm.self_complexity if cm.self_complexity is not None else 0.0
    signed_gap = self_cx - cm.derived_complexity
    last_gap = getattr(monitor, '_last_surfaced_complexity_gap', None)
    if last_gap is None or abs(signed_gap - last_gap) > _DIVERGENCE_NOVELTY_DELTA:
        monitor._last_surfaced_complexity_gap = signed_gap
        return True
    return False


def build_result(
    monitor,
    status: str,
    decision: Dict,
    metrics: Dict,
    confidence: float,
    confidence_metadata: Dict,
    task_type_adjustment,
    trajectory_validation,
    oscillation_state,
    response_tier: str,
    cirs_result,
    damping_result,
    behavioral_assessment=None,
) -> Dict:
    """Assemble the final result dict returned by process_update().

    Pure dict construction — no state mutations except drift telemetry recording.
    """
    from config.governance_config import GovernanceConfig as GovConfig

    result = {
        'status': status,
        'decision': decision,
        'policy_evaluation': _build_policy_evaluation(decision, metrics),
        'enforcement': _build_enforcement_stub(decision),
        'metrics': metrics,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'confidence_reliability': {
            'reliability': confidence_metadata.get('reliability', 'unknown'),
            'source': confidence_metadata.get('source', 'unknown'),
            'calibration_applied': confidence_metadata.get('calibration_applied', False),
            'calibration_samples': confidence_metadata.get('calibration_samples', 0),
            'external_provided': confidence_metadata.get('external_provided'),
            'derived_cap': confidence_metadata.get('derived_cap'),
            'honesty_note': confidence_metadata.get('honesty_note', 'No metadata available')
        }
    }

    if task_type_adjustment:
        result['task_type_adjustment'] = task_type_adjustment

    if trajectory_validation is not None:
        result['trajectory_validation'] = trajectory_validation

    # Dual-log continuity metrics
    if monitor._last_continuity_metrics:
        cm = monitor._last_continuity_metrics
        divergence_novel = _complexity_divergence_novel(monitor, cm)
        result['continuity'] = {
            'derived_complexity': cm.derived_complexity,
            'self_reported_complexity': cm.self_complexity,
            'complexity_divergence': cm.complexity_divergence,
            'divergence_novel': divergence_novel,
            'overconfidence_signal': cm.overconfidence_signal,
            'underconfidence_signal': cm.underconfidence_signal,
            'E_input': cm.E_input,
            'I_input': cm.I_input,
            'S_input': cm.S_input,
            'calibration_weight': cm.calibration_weight,
        }

    # Restorative balance status
    if monitor._last_restorative_status and monitor._last_restorative_status.needs_restoration:
        rs = monitor._last_restorative_status
        if monitor.state.update_count <= 3:
            result['restorative'] = {
                'needs_restoration': False,
                'suppressed': True,
                'note': 'Restorative guidance suppressed — not enough check-ins for reliable assessment.',
            }
        else:
            result['restorative'] = {
                'needs_restoration': rs.needs_restoration,
                'reason': rs.reason,
                'suggested_cooldown_seconds': rs.suggested_cooldown_seconds,
                'activity_rate': rs.activity_rate,
                'cumulative_divergence': rs.cumulative_divergence,
            }
            result['guidance'] = (
                f"Consider slowing down: {rs.reason}. "
                f"Suggested cooldown: {rs.suggested_cooldown_seconds}s"
            )

    # Concrete Ethical Drift
    if monitor._last_drift_vector:
        dv = monitor._last_drift_vector
        # #428: wrap each component with meaning + range + ideal at point-of-use.
        # `norm` and `norm_squared` aren't drift dimensions; they pass through
        # with just `value` via the helper.
        from src.governance_glossary import annotate_drift_components
        result['ethical_drift'] = annotate_drift_components({
            'calibration_deviation': dv.calibration_deviation,
            'complexity_divergence': dv.complexity_divergence,
            'coherence_deviation': dv.coherence_deviation,
            'stability_deviation': dv.stability_deviation,
            'norm': dv.norm,
            'norm_squared': dv.norm_squared,
        })

        try:
            record_drift(
                drift_vector=dv,
                agent_id=monitor.agent_id,
                update_count=monitor.state.update_count,
                baseline=get_agent_baseline(monitor.agent_id),
                decision=decision['action'],
                confidence=confidence,
            )
        except Exception as e:
            logger.debug(f"Failed to record drift telemetry: {e}")

    # HCK / CIRS metrics
    result['hck'] = {
        'rho': float(getattr(monitor.state, 'current_rho', 0.0)),
        'CE': float(monitor.state.CE_history[-1]) if monitor.state.CE_history else 0.0,
        'gains_modulated': getattr(monitor, '_gains_modulated', False)
    }

    if GovConfig.ADAPTIVE_GOVERNOR_ENABLED and monitor.adaptive_governor is not None:
        result['cirs'] = cirs_result
    else:
        result['cirs'] = {
            'oi': float(oscillation_state.oi),
            'flips': int(oscillation_state.flips),
            'resonant': bool(oscillation_state.resonant),
            'trigger': oscillation_state.trigger,
            'response_tier': response_tier,
            'resonance_events': int(getattr(monitor.state, 'resonance_events', 0)),
            'damping_applied_count': int(getattr(monitor.state, 'damping_applied_count', 0))
        }

        if damping_result and damping_result.damping_applied:
            result['cirs']['damping'] = {
                'd_tau': damping_result.adjustments.get('d_tau', 0),
                'd_beta': damping_result.adjustments.get('d_beta', 0)
            }

    # Behavioral EISV
    if behavioral_assessment is not None:
        # #428: wrap the behavioral verdict so the agent gets meaning +
        # next_action alongside the bare label. Pattern matches
        # ethical_drift wrapping above.
        from src.governance_glossary import explain_verdict
        result['behavioral'] = {
            'state': monitor._behavioral_state.to_dict(),
            'assessment': {
                'health': behavioral_assessment.health,
                'verdict': explain_verdict(behavioral_assessment.verdict),
                'risk': behavioral_assessment.risk,
                'coherence': behavioral_assessment.coherence,
                'components': behavioral_assessment.components,
                'guidance': behavioral_assessment.guidance,
            },
        }
        if monitor._behavioral_state.is_baselined and behavioral_assessment.health != "healthy":
            result['behavioral']['deviation'] = {
                'E': round(monitor._behavioral_state.deviation("E"), 2),
                'I': round(monitor._behavioral_state.deviation("I"), 2),
                'S': round(monitor._behavioral_state.deviation("S"), 2),
                'V': round(monitor._behavioral_state.deviation("V"), 2),
            }

    return result
