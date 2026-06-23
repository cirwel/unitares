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
        "next_action": "Open a dialectic review to resolve or contest it, or bring in a human.",
    },
    "uninitialized": {
        "meaning": "Agent has no recorded state yet.",
        "next_action": "Submit one process_agent_update to activate governance.",
    },
    "unbound": {
        "meaning": "No identity bound to this session.",
        "next_action": "Call onboard(force_new=true) to mint a fresh identity.",
    },
    "continue": {
        "meaning": "Synonym of proceed; legacy label still used in some payloads.",
        "next_action": "Continue working normally.",
    },
    # Behavioral-assessment verdicts (src/behavioral_assessment.py, monitor_decision.py).
    # Different vocabulary than the decision verdicts above but flows through the
    # same `metrics["verdict"]` field, so agents see both schemes.
    "safe": {
        "meaning": "Behavioral assessment: low risk.",
        "next_action": "Continue working normally.",
    },
    "caution": {
        "meaning": "Behavioral assessment: elevated risk — proceed deliberately.",
        "next_action": "Continue but watch coherence and risk_score; consider a check-in.",
    },
    "high-risk": {
        "meaning": "Behavioral assessment: significant risk.",
        "next_action": "Pause, reflect, or request dialectic review.",
    },
}


# -----------------------------------------------------------------------------
# BASINS — region of EISV state space
# -----------------------------------------------------------------------------

BASINS: Dict[str, Dict[str, Any]] = {
    "high": {
        "meaning": "Healthy. E and I are high; S and V are low. Normal operating range.",
        "thresholds": {
            "type": "all",
            "E_min": 0.6,
            "I_min": 0.7,
            "S_max": 0.25,
            "V_abs_max": 0.15,
            "coherence_min": 0.45,
            "risk_max": 0.45,
        },
    },
    "low": {
        "meaning": "Degraded. May need recovery or intervention.",
        "thresholds": {
            "type": "any",
            "I_below": 0.5,
            "coherence_below": 0.40,
            "V_abs_above": 0.30,
            "risk_at_or_above": 0.70,
        },
    },
    "boundary": {
        "meaning": "Transitioning between basins. Verdicts may carry margin: tight.",
        "thresholds": {
            "type": "complement",
            "rule": "Neither high nor low; transitional remainder of state space.",
        },
    },
    "critical": {
        "meaning": "Circuit breaker imminent. Pause and reassess.",
        "thresholds": {
            "type": "operator_alert",
            "rule": "Used by higher-level diagnostics when risk/coherence guards are near breaker thresholds.",
        },
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
# EISV_SOURCES — which state estimate is primary in read APIs
# -----------------------------------------------------------------------------

EISV_SOURCES: Dict[str, Dict[str, Any]] = {
    "behavioral": {
        "meaning": "Primary EISV comes from observed behavioral state.",
        "thresholds": {"behavioral_confidence_min": 0.3},
        "next_action": "Read behavioral_eisv first; use ode_eisv only as diagnostics.",
    },
    "ode_fallback": {
        "meaning": "Behavioral confidence is too low, so primary EISV falls back to ODE dynamics.",
        "thresholds": {"behavioral_confidence_below": 0.3},
        "next_action": "Treat flat EISV as a fallback estimate until behavioral observations accumulate.",
    },
}


# -----------------------------------------------------------------------------
# TRUST_TIERS — trajectory-identity tiers from compute_trust_tier
# -----------------------------------------------------------------------------
# Source: src/trajectory_identity.py:25-30 (_TRUST_TIER_NAMES) and the
# compute_trust_tier docstring (lines 737-741).

TRUST_TIERS: Dict[int, Dict[str, str]] = {
    0: {
        "name": "unknown",
        "meaning": "No trajectory data yet — identity has nothing to compare against.",
        "criteria": "Pre-genesis or trajectory metadata missing.",
    },
    1: {
        "name": "emerging",
        "meaning": "Identity is forming. Genesis is recorded but behavioral consistency is not yet established.",
        "criteria": "< 50 observations OR identity_confidence < 0.5.",
    },
    2: {
        "name": "established",
        "meaning": "Identity has consistent behavior across enough observations to be trustworthy.",
        "criteria": ">= 50 observations, identity_confidence >= 0.5, lineage_similarity > 0.7.",
    },
    3: {
        "name": "verified",
        "meaning": "Identity is robustly grounded — long-running and consistent.",
        "criteria": ">= 200 observations, identity_confidence >= 0.7, lineage_similarity > 0.8.",
    },
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


# Public process_agent_update accepts a positional ethical_drift vector. Keep
# this separate from concrete drift-vector telemetry above: these are the three
# caller-facing input slots from src/mcp_handlers/schemas/core.py.
ETHICAL_DRIFT_VECTOR_COMPONENTS: Dict[str, Dict[str, str]] = {
    "primary_drift": {
        "meaning": "Caller-reported primary ethical-drift pressure for this update.",
        "range": "[0, 1]",
        "ideal": "0",
    },
    "coherence_loss": {
        "meaning": "Caller-reported contribution from lost coherence or destabilized reasoning.",
        "range": "[0, 1]",
        "ideal": "0",
    },
    "complexity_contribution": {
        "meaning": "Caller-reported contribution from task complexity or overload.",
        "range": "[0, 1]",
        "ideal": "0",
    },
}


# -----------------------------------------------------------------------------
# TRAJECTORY_SIGNATURE_TERMS — opaque labels that appear inside trajectory data
# -----------------------------------------------------------------------------

TRAJECTORY_SIGNATURE_TERMS: Dict[str, Dict[str, Any]] = {
    "settling": {
        "meaning": "Projected state is moving toward an attractor or stable set point.",
        "next_action": "Usually safe to continue; watch for rising risk or entropy.",
    },
    "ode_fallback": EISV_SOURCES["ode_fallback"],
    "behavioral": EISV_SOURCES["behavioral"],
    "divergence": {
        "meaning": "Open exploration regime; higher entropy can be normal.",
        "next_action": "Keep scope visible and consolidate before switching to execution.",
    },
    "transition": {
        "meaning": "Between exploration and convergence; state may shift quickly.",
        "next_action": "Avoid raising complexity until coherence stabilizes.",
    },
    "convergence": {
        "meaning": "Focused regime; integrity and low entropy matter more than novelty.",
        "next_action": "Prefer verification and completion over new branches of work.",
    },
    "stable": {
        "meaning": "State is near equilibrium or recently steady.",
        "next_action": "Continue, while watching for hidden stasis if no progress is being made.",
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


def explain_eisv_source(source: Optional[str]) -> Dict[str, Any]:
    """Wrap a primary EISV source label with meaning + activation threshold."""
    return _wrap(source, EISV_SOURCES)


def explain_trajectory_signature_term(term: Optional[str]) -> Dict[str, Any]:
    """Wrap a string found in trajectory-signature payloads.

    The terms can come from several local vocabularies: state modes,
    trajectories, basins, primary EISV source labels, and CIRS regimes.
    """
    if term is None:
        return {"value": None}
    key = str(term)
    for table in (
        MODES,
        TRAJECTORIES,
        BASINS,
        EISV_SOURCES,
        TRAJECTORY_SIGNATURE_TERMS,
    ):
        if key in table:
            return {"value": term, **table[key]}
    return {"value": term, "meaning": "unknown (not in glossary)"}


_TIER_BY_NAME: Dict[str, int] = {info["name"]: tier_int for tier_int, info in TRUST_TIERS.items()}


def explain_trust_tier(tier: Optional[Any]) -> Dict[str, Any]:
    """Wrap a trust-tier value with name + meaning + criteria.

    Accepts:
      - None → {"value": None}
      - int 0-3 → full {tier, name, meaning, criteria} dict
      - name string ("emerging", "established", ...) → same as int via reverse lookup
      - {tier, name, reason} dict from compute_trust_tier → preserves all fields, adds meaning + criteria
      - any other value → {"value": <as-is>, "meaning": "unknown (not in glossary)"}

    Callers can drop this in place of the previous hardcoded
    {tier, name, reason} block without breaking consumers.
    """
    if tier is None:
        return {"value": None}
    # Already a dict — preserve all fields, add glossary annotation
    if isinstance(tier, dict):
        tier_value = tier.get("tier")
        if tier_value is None and "name" in tier:
            tier_value = _TIER_BY_NAME.get(tier["name"])
        info = TRUST_TIERS.get(tier_value) if tier_value is not None else None
        if info is None:
            return {**tier, "meaning": "unknown (not in glossary)"}
        merged = {**tier, "meaning": info["meaning"], "criteria": info["criteria"]}
        merged.setdefault("tier", tier_value)
        merged.setdefault("name", info["name"])
        return merged
    # Name string — reverse-lookup to int
    if isinstance(tier, str):
        tier_int = _TIER_BY_NAME.get(tier)
        if tier_int is None:
            return {"value": tier, "meaning": "unknown (not in glossary)"}
        info = TRUST_TIERS[tier_int]
        return {
            "tier": tier_int,
            "name": info["name"],
            "meaning": info["meaning"],
            "criteria": info["criteria"],
        }
    # Raw int
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        return {"value": tier, "meaning": "unknown (not in glossary)"}
    info = TRUST_TIERS.get(tier_int)
    if info is None:
        return {"tier": tier_int, "meaning": "unknown (not in glossary)"}
    return {
        "tier": tier_int,
        "name": info["name"],
        "meaning": info["meaning"],
        "criteria": info["criteria"],
    }


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


def explain_ethical_drift_vector(drift: Optional[Any]) -> Dict[str, Any]:
    """Name the three positional ethical_drift input components.

    Public schemas accept ethical_drift as
    [primary_drift, coherence_loss, complexity_contribution]. Returning both
    the original vector and named components preserves the wire shape while
    making the input self-describing at the response surface.
    """
    if drift is None:
        return {"value": None}
    try:
        values = list(drift)
    except TypeError:
        return {"value": drift, "meaning": "unknown (expected three-component vector)"}

    component_names = list(ETHICAL_DRIFT_VECTOR_COMPONENTS.keys())
    components: Dict[str, Dict[str, Any]] = {}
    for idx, name in enumerate(component_names):
        value = values[idx] if idx < len(values) else None
        components[name] = {
            "value": value,
            **ETHICAL_DRIFT_VECTOR_COMPONENTS[name],
        }
    extras = values[len(component_names):]
    result: Dict[str, Any] = {
        "value": values,
        "order": component_names,
        "components": components,
    }
    if extras:
        result["extra_components"] = [
            {"index": len(component_names) + i, "value": v}
            for i, v in enumerate(extras)
        ]
    return result


def annotate_trajectory_signature_terms(signature: Optional[Any]) -> Dict[str, Dict[str, Any]]:
    """Return glossary entries for known string values inside a signature.

    The original trajectory_signature can be large and structurally owned by
    upstream agents. This helper leaves it untouched and returns path-keyed
    annotations only for known terms.
    """
    if not isinstance(signature, dict):
        return {}

    annotations: Dict[str, Dict[str, Any]] = {}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")
        elif isinstance(value, str):
            explained = explain_trajectory_signature_term(value)
            if "meaning" in explained and not str(explained["meaning"]).startswith("unknown"):
                annotations[path] = explained

    walk(signature, "")
    return annotations
