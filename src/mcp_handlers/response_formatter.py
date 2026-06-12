"""
Response Formatter — Filters process_agent_update response by verbosity mode.

Extracted from core.py to reduce its size and make response modes independently testable.
"""

import os
from typing import Any

from src.logging_utils import get_logger
from src.monitor_result import DIVERGENCE_LINE_THRESHOLD
logger = get_logger(__name__)


def _copy_passthrough_fields(response_data: dict, result: dict, fields: tuple) -> None:
    """Copy named fields from response_data to result if present (not None).

    Empty containers (e.g., empty warnings list) are forwarded so callers
    can distinguish 'no warnings recorded' from 'warnings field absent.'
    """
    for field in fields:
        value = response_data.get(field)
        if value is not None:
            result[field] = value

def format_response(
    response_data: dict,
    arguments: dict,
    *,
    meta: Any = None,
    is_new_agent: bool = False,
    key_was_generated: bool = False,
    api_key_auto_retrieved: bool = False,
    task_type: str = "mixed",
) -> dict:
    """
    Apply response mode filtering to fully-built response_data.

    Priority: per-call response_mode > agent preferences > env var > auto

    Modes:
    - "auto": Select mode based on health_status
    - "standard"/"interpreted": Human-readable interpretation via GovernanceState
    - "minimal": Action + EISV snapshot + margin
    - "compact"/"lite": Brief metrics + decision summary
    - "full": No filtering (return as-is)

    Args:
        response_data: The complete response dict built by process_agent_update
        arguments: Original tool arguments (for response_mode param)
        meta: Agent metadata object (for preferences.verbosity)
        is_new_agent: Whether this is the agent's first check-in
        key_was_generated: Whether an API key was just generated
        api_key_auto_retrieved: Whether an API key was auto-retrieved
        task_type: Task type for state interpretation

    Returns:
        Filtered response_data dict
    """
    # Check agent preferences
    agent_verbosity_pref = None
    if meta and hasattr(meta, 'preferences') and meta.preferences:
        agent_verbosity_pref = meta.preferences.get("verbosity")

    # Preserve trust_tier across filtering. Save the full upstream dict (not
    # just the name) so terse modes can re-emit it through explain_trust_tier
    # at the agent-facing surface. #428: vocabulary at point-of-use.
    saved_trust_tier = None
    try:
        tier_obj = response_data.get("trajectory_identity", {}).get("trust_tier")
        if isinstance(tier_obj, dict):
            saved_trust_tier = tier_obj
        elif tier_obj is not None:
            saved_trust_tier = {"name": str(tier_obj)}
    except Exception:
        pass

    # Priority: per-call > agent pref > env var > auto
    response_mode = (
        arguments.get("response_mode") or
        agent_verbosity_pref or
        os.getenv("UNITARES_PROCESS_UPDATE_RESPONSE_MODE", "auto")
    ).strip().lower()

    using_default_mode = not arguments.get("response_mode") and not agent_verbosity_pref

    # Full mode: no filtering
    if response_mode == "full":
        return response_data

    # AUTO MODE: Adaptive verbosity based on health status
    if response_mode == "auto":
        metrics = response_data.get("metrics", {}) if isinstance(response_data.get("metrics"), dict) else {}
        health_status = (
            response_data.get("health_status") or
            metrics.get("health_status") or
            response_data.get("status") or
            "healthy"
        )
        # Auto-select mirror for disembodied agents (no sensor_data)
        has_sensor_data = response_data.get("_has_sensor_data", False)
        if health_status == "healthy" and not has_sensor_data:
            response_mode = "mirror"
        elif health_status == "healthy":
            response_mode = "minimal"
        elif health_status in ("at_risk", "critical"):
            response_mode = "standard"
        else:
            response_mode = "compact"

    # MIRROR MODE: Actionable self-awareness signals
    if response_mode == "mirror":
        response_data = _format_mirror(response_data, saved_trust_tier, meta=meta)

    # STANDARD MODE: Human-readable interpretation
    elif response_mode in ("standard", "interpreted"):
        response_data = _format_standard(response_data, task_type, saved_trust_tier)

    # MINIMAL MODE: Bare essentials
    elif response_mode == "minimal":
        response_data = _format_minimal(response_data, using_default_mode, saved_trust_tier)

    # COMPACT MODE: Brief metrics + decision
    elif response_mode in ("compact", "lite"):
        response_data = _format_compact(response_data, using_default_mode, saved_trust_tier)

    # Strip optional context for minimal/compact/mirror (reduce noise for established agents)
    if response_mode in ("minimal", "compact", "mirror"):
        _strip_context(response_data, is_new_agent, key_was_generated, api_key_auto_retrieved)

    return response_data

def _format_standard(response_data: dict, task_type: str, saved_trust_tier: Any = None) -> dict:
    """Build standard (interpreted) response."""
    from governance_state import GovernanceState
    from governance_core import State, Theta, DEFAULT_THETA

    metrics = response_data.get("metrics", {}) if isinstance(response_data.get("metrics"), dict) else {}
    decision = response_data.get("decision", {}) if isinstance(response_data.get("decision"), dict) else {}

    E = float(metrics.get("E", 0.7))
    I = float(metrics.get("I", 0.8))
    S = float(metrics.get("S", 0.1))
    V = float(metrics.get("V", 0.0))
    coherence = float(metrics.get("coherence", 0.5))
    risk_score = metrics.get("risk_score")

    temp_state = GovernanceState()
    temp_state.unitaires_state = State(E=E, I=I, S=S, V=V)
    temp_state.unitaires_theta = Theta(C1=DEFAULT_THETA.C1, eta1=DEFAULT_THETA.eta1)
    temp_state.coherence = coherence
    temp_state.decision_history = response_data.get("history", {}).get("decision_history", [])

    interpreted = temp_state.interpret_state(risk_score=risk_score, task_type=task_type)
    from src.governance_glossary import (
        explain_basin,
        explain_mode,
        explain_trajectory,
    )

    result = {
        "success": True,
        "agent_id": response_data.get("agent_id"),
        "decision": decision.get("action") or response_data.get("status"),
        "state": interpreted,
        "metrics": {
            "E": E, "I": I, "S": S, "V": V,
            "coherence": coherence, "risk_score": risk_score,
        },
        "_mode": "standard",
        "_raw_available": "Use response_mode='full' to see complete metrics",
    }
    state_glossary = {}
    if interpreted.get("mode") is not None:
        state_glossary["mode"] = explain_mode(interpreted.get("mode"))
    if interpreted.get("basin") is not None:
        state_glossary["basin"] = explain_basin(interpreted.get("basin"))
    if interpreted.get("trajectory") is not None:
        state_glossary["trajectory"] = explain_trajectory(interpreted.get("trajectory"))
    if state_glossary:
        result["state_glossary"] = state_glossary
    if saved_trust_tier:
        from src.governance_glossary import explain_trust_tier
        result["trust_tier"] = explain_trust_tier(saved_trust_tier)
    if "input_glossary" in response_data:
        result["input_glossary"] = response_data["input_glossary"]
    if "thread_context" in response_data:
        result["thread_context"] = response_data["thread_context"]
    if "identity_assurance" in response_data:
        result["identity_assurance"] = response_data["identity_assurance"]
    identity_notifications = response_data.get("_identity_notifications")
    if identity_notifications:
        result["identity_notifications"] = identity_notifications
    _copy_passthrough_fields(
        response_data,
        result,
        ("prediction_id", "warnings", "policy_evaluation", "enforcement"),
    )
    return result

def _format_mirror(response_data: dict, saved_trust_tier: Any, meta: Any = None) -> dict:
    """Build mirror response: a lens on the full data, not a filter that hides it."""
    decision = response_data.get("decision", {}) if isinstance(response_data.get("decision"), dict) else {}
    metrics = response_data.get("metrics", {}) if isinstance(response_data.get("metrics"), dict) else {}

    # #428: wrap the verdict with meaning + next_action at the response
    # surface. Internal consumers read decision["action"] / metrics["verdict"]
    # as bare strings; only the agent-facing payload key is wrapped.
    from src.governance_glossary import explain_verdict
    verdict_raw = decision.get("action", "continue")
    verdict = explain_verdict(verdict_raw)

    # Collect mirror signals from enrichment-produced data
    mirror_signals = list(response_data.get("_mirror_signals", []))

    # 1. Confidence reliability — surface source for transparency
    conf_rel = response_data.get("confidence_reliability", {})
    if isinstance(conf_rel, dict):
        if conf_rel.get("source") == "observed":
            mirror_signals.append(
                "No confidence reported — system derived from observed tool outcomes"
            )

    # 2. Calibration insights from learning_context
    learning_ctx = response_data.get("learning_context", {})
    if isinstance(learning_ctx, dict):
        cal = learning_ctx.get("calibration", {})
        if isinstance(cal, dict) and cal.get("insight"):
            insight = cal["insight"]
            # These figures come from calibration_checker.bin_stats, a module-
            # level singleton aggregated across ALL agents. The dashboard
            # labels this "Fleet-wide" (src/static/dashboard.js); the mirror
            # must match so agents don't mistake fleet trends for personal
            # history. See 2026-04-14 dogfood finding.
            if "INVERTED" in insight.upper():
                mirror_signals.insert(
                    0,
                    "Fleet confidence trending inverted "
                    "(high conf -> lower trajectory health across the fleet)",
                )
            elif cal.get("total_decisions", 0) >= 10:
                trajectory_health = cal.get("trajectory_health", cal.get("overall_accuracy", 0))
                high_conf_health = cal.get(
                    "high_confidence_trajectory_health",
                    cal.get("high_confidence_accuracy", "?"),
                )
                low_conf_health = cal.get(
                    "low_confidence_trajectory_health",
                    cal.get("low_confidence_accuracy", "?"),
                )
                # Only surface the fleet number when it is actually informative —
                # i.e. when fleet trajectory health is degrading. At steady-high
                # (~0.99 on every check-in) it is a constant dashboard stat with
                # zero per-turn signal that reads as a non-sequitur in a per-agent
                # mirror. The INVERTED case above still fires regardless.
                if isinstance(trajectory_health, (int, float)) and trajectory_health < 0.95:
                    mirror_signals.append(
                        f"Fleet calibration degrading: {trajectory_health:.0%} trajectory health over "
                        f"{cal['total_decisions']} fleet-wide decisions "
                        f"(high-conf health: {high_conf_health}, "
                        f"low-conf health: {low_conf_health})"
                    )

    # 3. Complexity divergence — suppress on first few check-ins (no baseline)
    update_count = getattr(meta, 'total_updates', 999) if meta else 999
    if update_count <= 3:
        pass  # Not enough history for meaningful complexity comparison
    else:
        continuity = response_data.get("continuity", {})
        if (
            isinstance(continuity, dict)
            and continuity.get("complexity_divergence", 0) > DIVERGENCE_LINE_THRESHOLD
        ):
            # Novelty gate (monitor_result computes it from the signed gap):
            # surface the line on the first crossing or when the gap
            # materially changes, not on every check-in of a stable
            # session-long gap (dogfood 2026-06-10). Payloads without the
            # key (older builders, hand-built dicts) keep the raw-threshold
            # behavior.
            divergence_novel = continuity.get("divergence_novel")
            if divergence_novel is None:
                divergence_novel = True
            if divergence_novel:
                reported = continuity.get("self_reported_complexity", 0)
                derived = continuity.get("derived_complexity", 0)
                divergence = continuity.get("complexity_divergence", 0)
                # Neutral, recorded observation — not an interrogation. The
                # derived value is a proxy, so name its basis inline: it
                # measures output surface (response length/structure + tool
                # mix), not task content. The self-report is the richer
                # signal; a divergence is calibration data, not a demand to
                # justify "difficulty" on an otherwise-healthy check-in.
                mirror_signals.append(
                    f"Complexity calibration: you reported {reported:.2f}; output-surface "
                    f"estimate {derived:.2f} (Δ{divergence:.2f}) — estimate reads response "
                    f"length/structure + tool mix, not task content; logged for the "
                    f"calibration curve."
                )
        else:
            # Fallback to calibration_feedback if continuity not present
            cal_feedback = response_data.get("calibration_feedback", {})
            if isinstance(cal_feedback, dict):
                complexity_info = cal_feedback.get("complexity", {})
                if isinstance(complexity_info, dict) and complexity_info.get("discrepancy", 0) > 0.3:
                    reported = complexity_info.get("reported", 0)
                    derived = complexity_info.get("derived", 0)
                    mirror_signals.append(
                        f"Complexity calibration: you reported {reported:.2f}; output-surface "
                        f"estimate {derived:.2f} — estimate reads response length/structure "
                        f"+ tool mix, not task content; logged for the calibration curve."
                    )

    # 4. Pace — reflect cooldown-threshold state descriptively. NOT "Restorative
    # action:" — that named an action (the verdict's voice). The mirror reflects
    # the pace facts; the decision to cool down, if any, is the verdict's job.
    restorative = response_data.get("restorative", {})
    if isinstance(restorative, dict) and restorative.get("needs_restoration"):
        restorative_reason = restorative.get("reason")
        restorative_reasons = restorative.get("reasons", [])
        if isinstance(restorative_reason, str) and restorative_reason:
            mirror_signals.append(f"Pace: {restorative_reason}")
        elif restorative_reasons:
            mirror_signals.append(f"Pace: {'; '.join(str(r) for r in restorative_reasons[:2])}")

    # 5. Surface relevant KG discoveries — from mirror enrichment AND from existing enrichments
    relevant_prior = []
    kg_results = response_data.get("_mirror_kg_results", [])
    # Also pull from existing relevant_discoveries enrichment
    existing_discoveries = response_data.get("relevant_discoveries", [])
    if isinstance(existing_discoveries, list):
        for disc in existing_discoveries:
            if isinstance(disc, dict):
                kg_results.append(disc)
    for disc in kg_results[:5]:
        entry = {
            "summary": disc.get("summary", "")[:200],
            "by": disc.get("agent_id", "unknown"),
        }
        relevance = disc.get("relevance", disc.get("score", 0))
        if relevance:
            entry["relevance"] = relevance
        relevant_prior.append(entry)

    # 6. Surface the single most relevant reflection when state warrants it — a
    # descriptive lens, not a directive question (reflect, don't advise).
    reflection = response_data.get("_mirror_reflection")
    if reflection is None:
        # Back-compat: older enrichments may still set _mirror_question.
        reflection = response_data.get("_mirror_question", None)

    result = {
        "success": True,
        "verdict": verdict,
        "_mode": "mirror",
        "mirror": mirror_signals if mirror_signals else ["No actionable signals — steady state"],
    }

    # Proprioceptive numbers: phi is the primary basin discriminator
    # (empirical, 2026-04-30) and coherence/risk are the pair the verdict
    # reasons over. Without them a mirror-mode agent needs a second tool
    # call (get_governance_metrics) to learn its own state. Data, not a
    # signal line — they sit top-level beside margin/nearest_edge, never
    # in the prose signals (dogfood 2026-06-10). NOT named "state": the
    # full payload has a deliberately-stripped duplicate key of that name.
    # "Don't chase a number" still holds: these are governed-over values
    # the agent cannot write, only influence through behavior — gaming
    # coherence requires actually behaving more coherently.
    for state_key, state_val in (
        ("phi", metrics.get("phi")),
        ("coherence", metrics.get("coherence")),
        ("risk_score", metrics.get("risk_score")),
    ):
        if state_val is not None:
            result[state_key] = state_val

    if relevant_prior:
        result["relevant_prior_work"] = relevant_prior

    if reflection:
        result["reflection"] = reflection

    # Include margin/edge warnings
    margin = decision.get("margin")
    if margin is not None:
        if isinstance(margin, str) or (isinstance(margin, (int, float)) and margin < 0.1):
            result["margin"] = margin
            result["nearest_edge"] = decision.get("nearest_edge")

    if saved_trust_tier:
        # #428: wrap with glossary so agent sees tier scale + meaning inline.
        from src.governance_glossary import explain_trust_tier
        result["trust_tier"] = explain_trust_tier(saved_trust_tier)
    if "thread_context" in response_data:
        result["thread_context"] = response_data["thread_context"]
    if "identity_assurance" in response_data:
        result["identity_assurance"] = response_data["identity_assurance"]

    # Include identity notifications if present
    identity_notifications = response_data.get("_identity_notifications")
    if identity_notifications:
        result["identity_notifications"] = identity_notifications

    _copy_passthrough_fields(
        response_data,
        result,
        ("prediction_id", "warnings", "policy_evaluation", "enforcement"),
    )
    return result


def _format_minimal(response_data: dict, using_default_mode: bool, saved_trust_tier: Any) -> dict:
    """Build minimal response: action + EISV + margin."""
    decision = response_data.get("decision", {}) if isinstance(response_data.get("decision"), dict) else {}
    metrics = response_data.get("metrics", {}) if isinstance(response_data.get("metrics"), dict) else {}

    result = {
        "action": decision.get("action", "continue"),
        "_mode": "minimal",
        "E": metrics.get("E"),
        "I": metrics.get("I"),
        "S": metrics.get("S"),
        "V": metrics.get("V"),
        "coherence": metrics.get("coherence"),
        # phi is the primary basin discriminator (empirical, 2026-04-30);
        # minimal carried every EISV channel except it. Compact already
        # includes it — parity, not a new surface.
        "phi": metrics.get("phi"),
        "risk_score": metrics.get("risk_score"),
        "risk_score_latest": metrics.get("latest_risk_score"),
    }

    margin = decision.get("margin")
    if margin:
        result["margin"] = margin
    nearest_edge = decision.get("nearest_edge")
    if nearest_edge:
        result["nearest_edge"] = nearest_edge
    if using_default_mode:
        result["_tip"] = "Set verbosity: update_agent_metadata(preferences={'verbosity':'minimal'})"
    if saved_trust_tier:
        # Minimal mode is intentionally terse — emit the name string only.
        # Agents wanting tier-scale + meaning should use mirror/compact.
        result["trust_tier"] = (
            saved_trust_tier.get("name") if isinstance(saved_trust_tier, dict) else saved_trust_tier
        )
    if "thread_context" in response_data:
        result["thread_context"] = response_data["thread_context"]
    if "identity_assurance" in response_data:
        result["identity_assurance"] = response_data["identity_assurance"]
    identity_notifications = response_data.get("_identity_notifications")
    if identity_notifications:
        result["identity_notifications"] = identity_notifications
    _copy_passthrough_fields(response_data, result, ("warnings",))

    return result

def _format_compact(response_data: dict, using_default_mode: bool, saved_trust_tier: Any) -> dict:
    """Build compact response: brief metrics + decision summary."""
    metrics = response_data.get("metrics", {}) if isinstance(response_data.get("metrics"), dict) else {}
    decision = response_data.get("decision", {}) if isinstance(response_data.get("decision"), dict) else {}

    # `metrics.risk_score` is the smoothed gating value (mean of last 10
    # observations) — the same number `make_decision` reasoned over.
    # `latest_risk_score` is the raw most-recent observation; surface it
    # alongside so readers can see both the gating signal and the spike.
    canonical_risk = metrics.get("risk_score")
    latest_risk = metrics.get("latest_risk_score")

    # #428: wrap verdict with meaning + next_action at the response surface.
    # The bare metrics["verdict"] is preserved for internal readers; this is
    # a new dict consumed only by the agent-facing payload.
    from src.governance_glossary import explain_verdict
    compact_metrics = {
        "E": metrics.get("E"),
        "I": metrics.get("I"),
        "S": metrics.get("S"),
        "V": metrics.get("V"),
        "coherence": metrics.get("coherence"),
        "risk_score": canonical_risk,
        "risk_score_latest": latest_risk,
        "phi": metrics.get("phi"),
        "verdict": explain_verdict(metrics.get("verdict")),
        "lambda1": metrics.get("lambda1"),
        "health_status": metrics.get("health_status"),
        "health_message": metrics.get("health_message"),
    }

    compact_decision = {
        "action": decision.get("action"),
        "reason": decision.get("reason"),
        "require_human": decision.get("require_human"),
        "margin": decision.get("margin"),
        "nearest_edge": decision.get("nearest_edge"),
    }

    health_status = response_data.get("health_status") or compact_metrics.get("health_status") or response_data.get("status")
    coherence = compact_metrics.get("coherence")
    risk_val = compact_metrics.get("risk_score")
    action = compact_decision.get("action") or response_data.get("status")
    summary = f"{action} | health={health_status} | coherence={coherence} | risk_score={risk_val}"

    result = {
        "success": True,
        "agent_id": response_data.get("agent_id"),
        "status": response_data.get("status"),
        "health_status": health_status,
        "health_message": response_data.get("health_message"),
        "decision": compact_decision,
        "metrics": compact_metrics,
        "summary": summary,
        "_mode": "compact",
    }

    if saved_trust_tier:
        # #428: glossary at point-of-use — tier scale + meaning inline.
        from src.governance_glossary import explain_trust_tier
        result["trust_tier"] = explain_trust_tier(saved_trust_tier)
    if using_default_mode:
        result["_tip"] = "Verbosity options: response_mode='minimal'|'compact'|'full', or set permanently via update_agent_metadata(preferences={'verbosity':'minimal'})"
    if "thread_context" in response_data:
        result["thread_context"] = response_data["thread_context"]
    if "identity_assurance" in response_data:
        result["identity_assurance"] = response_data["identity_assurance"]
    identity_notifications = response_data.get("_identity_notifications")
    if identity_notifications:
        result["identity_notifications"] = identity_notifications

    _copy_passthrough_fields(
        response_data,
        result,
        ("prediction_id", "warnings", "policy_evaluation", "enforcement"),
    )
    return result

def _strip_context(response_data: dict, is_new_agent: bool, key_was_generated: bool, api_key_auto_retrieved: bool):
    """Strip optional context fields for minimal/compact/mirror modes (in-place)."""
    # Unconditional strips (always noise in filtered modes)
    response_data.pop("eisv_labels", None)
    # Internal mirror signals (consumed during _format_mirror, not needed after)
    response_data.pop("_mirror_signals", None)
    response_data.pop("_mirror_kg_results", None)
    response_data.pop("_mirror_question", None)
    response_data.pop("_mirror_reflection", None)
    response_data.pop("_has_sensor_data", None)
    response_data.pop("_eisv_validation_warning", None)

    # Strip empty advisories (non-empty ones are kept)
    advisories = response_data.get("advisories")
    if isinstance(advisories, list) and len(advisories) == 0:
        response_data.pop("advisories", None)

    if not is_new_agent:
        response_data.pop("learning_context", None)
        response_data.pop("relevant_discoveries", None)
        response_data.pop("onboarding", None)
        response_data.pop("welcome", None)

        # Enrichment bloat — heavy nested dicts with low signal for established agents
        response_data.pop("convergence_guidance", None)
        response_data.pop("calibration_feedback", None)
        response_data.pop("trajectory_identity", None)
        response_data.pop("drift_forecast", None)
        response_data.pop("saturation_diagnostics", None)
        response_data.pop("perturbation", None)
        response_data.pop("actionable_feedback", None)
        response_data.pop("state", None)

        # CIRS internals
        response_data.pop("cirs_void_alert", None)
        response_data.pop("cirs_state_announce", None)
        response_data.pop("outcome_event", None)

        # Low-value for established agents
        response_data.pop("temporal_context", None)
        response_data.pop("identity_reminder", None)
        response_data.pop("unitares_v41", None)
        response_data.pop("pending_dialectic", None)

        # Coaching (only valuable on non-proceed, but strip unconditionally in filtered modes)
        response_data.pop("llm_coaching", None)
        response_data.pop("recovery_coaching", None)

        if not (key_was_generated or api_key_auto_retrieved):
            response_data.pop("api_key_hint", None)
            response_data.pop("_onboarding", None)
