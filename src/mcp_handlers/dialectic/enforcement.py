"""
Dialectic Condition Enforcement

Enforces conditions from resolved dialectic sessions on agent check-ins.
Conditions are stored in meta.dialectic_conditions and checked against
computed metrics after the ODE step.

Condition types:
- complexity_limit: Caps input complexity before ODE (pre-ODE enforcement)
- risk_target: Escalates verdict if risk exceeds target (post-ODE enforcement)
- coherence_target: Retired compatibility condition; never escalates
- monitoring_duration: Time-bounds other conditions; expires them after duration
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Verdict severity ordering for escalation
_VERDICT_ORDER = {"proceed": 0, "guide": 1, "pause": 2, "reject": 3}


def _is_expired(condition: dict) -> bool:
    """Check if a condition has expired based on monitoring_duration or explicit expiry."""
    if condition.get("expired"):
        return True

    applied_at = condition.get("applied_at")
    duration_hours = condition.get("duration_hours")

    if applied_at and duration_hours is not None:
        try:
            applied = datetime.fromisoformat(applied_at)
            if applied.tzinfo is None:
                applied = applied.replace(tzinfo=timezone.utc)
            elapsed_hours = (datetime.now(timezone.utc) - applied).total_seconds() / 3600
            if elapsed_hours >= duration_hours:
                condition["expired"] = True
                return True
        except (ValueError, TypeError):
            pass

    return False


def _get_monitoring_duration(conditions: List[dict]) -> Optional[float]:
    """Find the active monitoring_duration from conditions, if any."""
    for c in conditions:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "monitoring_duration" and not _is_expired(c):
            return c.get("value")
    return None


def _apply_duration_to_conditions(conditions: List[dict]) -> None:
    """Propagate monitoring_duration to sibling conditions that lack their own expiry."""
    duration_hours = _get_monitoring_duration(conditions)
    if duration_hours is None:
        return

    for c in conditions:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type", "")
        if ctype == "monitoring_duration":
            continue
        # Only add duration if condition doesn't already have one
        if c.get("duration_hours") is None and c.get("applied_at"):
            c["duration_hours"] = duration_hours


def _escalate(current_action: str, to_action: str) -> str:
    """Escalate verdict only if the target is more severe."""
    current_level = _VERDICT_ORDER.get(current_action, 0)
    target_level = _VERDICT_ORDER.get(to_action, 0)
    return to_action if target_level > current_level else current_action


def enforce_post_ode_conditions(
    conditions: List[dict],
    metrics: Dict[str, Any],
    decision: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Enforce risk targets; retire unprovenanced coherence targets.

    Args:
        conditions: List of condition dicts from meta.dialectic_conditions
        metrics: Computed EISV metrics dict (has 'risk_score', 'coherence', etc.)
        decision: Current decision dict (has 'action', 'reason', etc.)

    Returns:
        (possibly-modified decision dict, list of warning strings)
    """
    if not conditions:
        return decision, []

    # Propagate monitoring_duration to sibling conditions
    _apply_duration_to_conditions(conditions)

    warnings: List[str] = []
    risk_score = metrics.get("risk_score", 0.0)
    current_action = decision.get("action", "proceed")
    escalated = False

    for c in conditions:
        if not isinstance(c, dict):
            continue
        if _is_expired(c):
            continue

        ctype = c.get("type", "")
        value = c.get("value")
        if not isinstance(value, (int, float)):
            continue

        if ctype == "risk_target":
            if risk_score > value:
                overshoot = risk_score - value
                # Severe overshoot (>50% of target) → pause; otherwise → guide
                if overshoot > value * 0.5:
                    new_action = _escalate(current_action, "pause")
                else:
                    new_action = _escalate(current_action, "guide")

                if new_action != current_action:
                    current_action = new_action
                    escalated = True

                warnings.append(
                    f"Dialectic condition: risk {risk_score:.2f} exceeds target {value:.2f}"
                )

        elif ctype == "coherence_target":
            # Stored conditions have no producer provenance. They therefore
            # cannot distinguish behavioral update consistency from legacy C(V)
            # control feedback. Fail closed on epistemic authority: retire the
            # condition in place and surface one warning, without changing the
            # decision.
            c["expired"] = True
            c["retired"] = True
            c["retired_reason"] = "coherence_producer_unprovenanced"
            warnings.append(
                "Dialectic coherence_target retired without enforcement: "
                "the stored condition has no producer provenance"
            )

    if escalated:
        decision = dict(decision)  # Shallow copy to avoid mutating original
        decision["action"] = current_action
        original_reason = decision.get("reason", "")
        decision["reason"] = f"{original_reason} [escalated by dialectic conditions]"
        decision["dialectic_escalated"] = True

    return decision, warnings


def enforce_complexity_limit(
    conditions: List[dict],
    complexity: float,
) -> Tuple[float, Optional[str]]:
    """Enforce complexity_limit conditions by capping the input value.

    Args:
        conditions: List of condition dicts from meta.dialectic_conditions
        complexity: Current complexity value

    Returns:
        (possibly-capped complexity, warning string or None)
    """
    if not conditions:
        return complexity, None

    caps: List[float] = []
    for c in conditions:
        if not isinstance(c, dict):
            continue
        if _is_expired(c):
            continue

        ctype = c.get("type", "")
        if ctype == "complexity_limit":
            v = c.get("value")
            if isinstance(v, (int, float)):
                caps.append(float(v))
        elif ctype == "complexity_adjustment" and c.get("action") == "reduce":
            v = c.get("target_value")
            if isinstance(v, (int, float)):
                caps.append(float(v))

    # Sanity check range
    caps = [v for v in caps if 0.0 <= v <= 1.0]

    if caps:
        cap = min(caps)
        if complexity > cap:
            warning = (
                f"Dialectic condition enforced: complexity {complexity:.2f} "
                f"capped to {cap:.2f}"
            )
            return cap, warning

    return complexity, None
