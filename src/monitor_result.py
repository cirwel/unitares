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


def _build_policy_evaluation(decision: Dict, metrics: Dict,
                             latest_risk: float = None) -> Dict:
    """Describe the policy layer that consumed measurements.

    EISV/risk/coherence are instrument readings. ``monitor_decision`` is the
    policy layer that maps those readings to guidance/action. Keeping this
    envelope distinct from ``decision`` lets clients and operators inspect the
    policy without treating the measurement itself as the actuator.

    ``risk_score`` here is the value the gate ran on; ``risk_score_latest`` is
    the most-recent raw observation. Surfacing both makes the relationship
    explicit so a reader can never conclude "the policy never sees the latest
    value" (F2) — and the fast-trip below acts on it.
    """
    inputs = {
        "basin": decision.get("basin"),
        **{field: metrics.get(field) for field in _POLICY_INPUT_FIELDS},
        "risk_score_latest": (
            latest_risk if latest_risk is not None
            else metrics.get("latest_risk_score")
        ),
        "margin": decision.get("margin"),
        "nearest_edge": decision.get("nearest_edge"),
    }
    evaluation = {
        "policy_name": "monitor_decision",
        "policy_version": "v1",
        "action": decision.get("action"),
        "sub_action": decision.get("sub_action"),
        "reason": decision.get("reason"),
        "guidance": decision.get("guidance"),
        "inputs": inputs,
        "measurement_role": "EISV/risk/coherence are policy inputs, not the actuator itself.",
    }
    if decision.get("latest_risk_fast_trip"):
        evaluation["latest_risk_fast_trip"] = decision["latest_risk_fast_trip"]
    return evaluation


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
            "Policy requested enforcement. This envelope is the pre-actuation "
            "candidate; the authenticated update boundary applies it as a circuit "
            "breaker (agent metadata -> status=paused, blocking later writes) and "
            "overwrites this with applied=true. A non-actuating path (e.g. "
            "simulate) leaves it unapplied."
            if requested else
            "No enforcement requested by policy."
        ),
    }


def _build_risk_attribution(
    metrics: Dict,
    drift_vector,
    continuity_metrics,
    behavioral_assessment,
    baseline_status: Dict = None,
) -> Dict:
    """Decompose the risk/verdict by signal provenance.

    The verdict path is a self-report integrity mechanism, not an
    adversary-resistant monitor: risk is driven primarily by the
    caller-supplied ``ethical_drift`` vector (it dominates the phi-based
    score) plus self-reported complexity/confidence. The only
    non-self-attested signal is the behavioral text model, and at current
    maturity it does not carry enforcement weight (verification layer
    reserved for v2). Surfacing the decomposition lets a reader see *what*
    drove the verdict and how much of it is self-attested vs measured
    (dogfood 2026-06-13, P0). All inputs here are already computed elsewhere
    in the result; this only re-exposes them grouped by provenance.
    """
    self_reported: Dict = {
        "provenance": "self_attested",
        "description": (
            "Magnitude of the caller-supplied ethical_drift vector — the "
            "dominant input to the phi-based risk score. Reported by the agent, "
            "not independently verified."
        ),
        "ethical_drift_norm": (
            float(drift_vector.norm)
            if drift_vector is not None and hasattr(drift_vector, "norm")
            else None
        ),
    }

    derived: Dict = {
        "provenance": "derived",
        "description": (
            "Gap between self-reported and server-derived complexity. Derived "
            "from inputs, but the self-reported half is still caller-supplied."
        ),
        "complexity_divergence": (
            float(continuity_metrics.complexity_divergence)
            if continuity_metrics is not None
            and hasattr(continuity_metrics, "complexity_divergence")
            else None
        ),
    }

    behavioral: Dict = {
        "provenance": "measured",
        "description": (
            "Per-agent behavioral EISV assessment (EMA z-scores vs this agent's "
            "own baseline, plus tool-outcome signals) — the least self-attested "
            "input. It IS combined into the verdict once the behavioral state is "
            "warm (confidence >= 0.3): the enforcement pair takes the more-severe "
            "verdict and the max risk, so it can escalate but not erase Φ. Before "
            "warmup it is telemetry-only. A stronger verification/adversarial "
            "weighting (the real fix for drift-discriminability) is reserved for v2."
        ),
        "risk": (
            float(behavioral_assessment.risk)
            if behavioral_assessment is not None
            else None
        ),
        "verdict": (
            behavioral_assessment.verdict
            if behavioral_assessment is not None
            else None
        ),
    }

    attribution = {
        "risk_score": metrics.get("risk_score"),
        "verdict": metrics.get("verdict"),
        "primary_driver": "self_reported",
        "note": (
            "At current maturity this verdict is driven primarily by signals you "
            "reported (ethical_drift, complexity, confidence). An agent that "
            "under-reports ethical_drift lowers Φ-based risk regardless of its "
            "actual behavior. The per-agent behavioral signal is the least "
            "self-attested input; once warm (confidence >= 0.3) it is combined "
            "into the verdict and can escalate it (more-severe verdict, max risk), "
            "but it cannot lower a worse Φ and is not yet the primary driver — "
            "stronger verification-weighted behavioral scoring is reserved for v2."
        ),
        "sources": {
            "self_reported": self_reported,
            "derived": derived,
            "behavioral": behavioral,
        },
    }

    # F1(b): during behavioral bootstrap the phi-based risk keys on
    # baseline-deviation terms that sit near zero, so risk_score is NOT
    # discriminative of absolute drift magnitude — it does not move under a
    # worsening drift vector. Flag it explicitly (mirroring restorative's
    # `suppressed` pattern) rather than letting a reader treat a confident
    # margin-to-PAUSE as meaningful in this window. The real fix is a
    # baseline-independent drift floor + v2 behavioral weighting (F1a / v2).
    if baseline_status is not None:
        is_baselined = bool(baseline_status.get("is_baselined", False))
        completed = baseline_status.get("updates_completed")
        target = baseline_status.get("baseline_target")
        until = None
        if isinstance(completed, int) and isinstance(target, int):
            until = max(0, target - completed)
        attribution["discriminability"] = {
            "baselined": is_baselined,
            "non_discriminative": not is_baselined,
            "updates_completed": completed,
            "baseline_target": target,
            "updates_until_baseline": until,
            "note": (
                None if is_baselined else
                "Behavioral baseline not yet established — risk_score keys on "
                "baseline-deviation terms that are ~0 during bootstrap and does "
                "not track absolute drift magnitude. Treat risk_score (and any "
                "margin-to-PAUSE derived from it) as non-discriminative until "
                "baselined; rely on the reported ethical_drift norm directly."
            ),
        }

    return attribution


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
        'policy_evaluation': _build_policy_evaluation(
            decision, metrics,
            latest_risk=(
                float(monitor.state.risk_history[-1])
                if getattr(getattr(monitor, 'state', None), 'risk_history', None)
                else None
            ),
        ),
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

    # Verdict provenance: decompose risk by signal source so a reader can see
    # how much of the verdict is self-attested vs measured (dogfood P0).
    _beh = getattr(monitor, '_behavioral_state', None)
    _baseline_status = None
    if _beh is not None:
        from src.behavioral_state import BASELINE_WARMUP_UPDATES
        _baseline_status = {
            "is_baselined": _beh.is_baselined,
            "updates_completed": getattr(_beh, 'update_count', None),
            "baseline_target": BASELINE_WARMUP_UPDATES,
        }
    result['risk_attribution'] = _build_risk_attribution(
        metrics,
        monitor._last_drift_vector,
        monitor._last_continuity_metrics,
        behavioral_assessment,
        baseline_status=_baseline_status,
    )

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
