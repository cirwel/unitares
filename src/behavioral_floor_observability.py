"""Decision-neutral observability for behavioral absolute-floor geometry.

The absolute EISV floors in :mod:`src.behavioral_assessment` bound individual
risk components; they do not force a behavioral verdict.  This module records
that geometry after assessment so production rows can answer issue #1995's
calibration question without changing risk, verdict, policy, or enforcement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from src import behavioral_assessment

if TYPE_CHECKING:
    from src.behavioral_assessment import AssessmentResult
    from src.behavioral_state import BehavioralEISV


ABSOLUTE_FLOOR_OBSERVATION_SCHEMA = "behavioral.absolute_floor_observation.v1"

_COMPONENT_DIMENSIONS = (
    ("low_E", "E"),
    ("low_I", "I"),
    ("high_S", "S"),
    ("high_V", "V"),
)


def build_absolute_floor_observation(
    state: BehavioralEISV,
    assessment: AssessmentResult,
    *,
    measurement_scope: str = "live",
    resolved_verdict_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe floor breaches without participating in assessment or policy.

    Every evaluated check-in returns the same bounded schema, including an
    empty breach list.  That zero-inclusive denominator distinguishes "no
    breach" from missing telemetry.  Comparisons intentionally match
    ``_score_absolute_floors`` exactly: equality is not a breach.
    """

    floor_components = behavioral_assessment._score_absolute_floors(state)
    breached_dimensions = [
        dimension
        for component, dimension in _COMPONENT_DIMENSIONS
        if component in floor_components
    ]

    observation = {
        "schema": ABSOLUTE_FLOOR_OBSERVATION_SCHEMA,
        "evaluated": True,
        "measurement_role": "telemetry_only",
        "policy_effect": "none",
        "measurement_scope": measurement_scope,
        "eligible_for_production_counter": measurement_scope == "live",
        "threshold_snapshot": {
            "E_breach_lt": behavioral_assessment.ABSOLUTE_E_FLOOR,
            "I_breach_lt": behavioral_assessment.ABSOLUTE_I_FLOOR,
            "S_breach_gt": behavioral_assessment.ABSOLUTE_S_CEILING,
            "abs_V_breach_gt": behavioral_assessment.ABSOLUTE_V_CEILING,
            "safe_risk_lt": behavioral_assessment.RISK_SAFE_THRESHOLD,
            "caution_risk_lt": behavioral_assessment.RISK_CAUTION_THRESHOLD,
        },
        "behavioral_confidence": float(state.confidence),
        "behavioral_baselined": bool(state.is_baselined),
        "resolved_verdict_source": resolved_verdict_source,
        "behavioral_verdict_authoritative": (
            resolved_verdict_source == "behavioral_assessment"
        ),
        "breached_dimensions": breached_dimensions,
        "breach_count": len(breached_dimensions),
        "behavioral_risk": float(assessment.risk),
        "behavioral_verdict": assessment.verdict,
        "breach_with_safe_behavioral_verdict": bool(
            breached_dimensions and assessment.verdict == "safe"
        ),
    }
    if breached_dimensions:
        # Keep the zero-inclusive denominator row lean; raw measurements and
        # component contributions are useful only when there is a breach to
        # explain.  Both dictionaries have fixed, four-dimension vocabularies.
        observation["breach_measurement_snapshot"] = {
            "E": float(state.E),
            "I": float(state.I),
            "S": float(state.S),
            "V": float(state.V),
        }
        observation["floor_component_contributions"] = {
            component: float(floor_components[component])
            for component, _dimension in _COMPONENT_DIMENSIONS
            if component in floor_components
        }
    return observation
