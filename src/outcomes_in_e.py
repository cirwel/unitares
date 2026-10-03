"""Agent-facing account of the outcome term in behavioral E (#2610).

When an agent has at least three recorded outcomes in the last 24h, 20% of each
behavioral E observation is ``outcome_success``, a linear map of the share of
those outcomes that were not adverse. Nothing in the check-in response said so:
an agent whose E fell after a failed test or a failed task could not tell that
the failure was the reason, which of its outcomes counted, or how much one
adverse outcome moves the term.

This module reads the derivation the check-in already built
(``eisv_telemetry.build_behavioral_derivation``) and restates the outcome part
of it. It computes nothing new and changes no input: the term's value, weight
and the outcomes come from the same record the formula produced, so the
account cannot drift from what was scored. Measurement-only; nothing in the
policy path reads it.

Two facts the account always carries, because the formula does not record
them: outcomes count as recorded, without attribution to whether this agent's
change caused them, and an adverse outcome means rework was needed, not fault
(see the 0.3.1 admission note in
docs/proposals/eisv-incremental-value-ablation-v1.md). Changing either is the
formula change #2610 defers until after the 2026-12-01 registered read.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from src.behavioral_sensor import OUTCOME_E_FLOOR, OUTCOME_E_SPAN

OUTCOME_COMPONENT = "outcome_success"
WINDOW_TEXT = "last 24h, at most 20 outcomes"
NOTE = (
    "Outcomes count as recorded: whether your change caused them is not "
    "tracked, and adverse means rework was needed, not fault. E is a smoothed "
    "average of observations, so one observation moves it only partly."
)


def _e_record(derivation: Mapping[str, Any]) -> Mapping[str, Any] | None:
    components = derivation.get("components")
    dimensions = components.get("dimensions") if isinstance(components, Mapping) else None
    record = dimensions.get("E") if isinstance(dimensions, Mapping) else None
    return record if isinstance(record, Mapping) else None


def _outcome_component(e_record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for component in e_record.get("components") or ():
        if isinstance(component, Mapping) and component.get("name") == OUTCOME_COMPONENT:
            return component
    return None


def _retained_after_adjustments(e_record: Mapping[str, Any]) -> float:
    """Share of the base value that survives the later E blends.

    The base E (where the outcome term lives) is blended afterwards with the
    response-structure and tool-success inputs, each keeping
    ``retained_weight`` of what came before. The term's real share of the
    observation is its weight times their product, not its nominal weight.
    """
    retained = 1.0
    for adjustment in e_record.get("adjustments") or ():
        if isinstance(adjustment, Mapping):
            retained *= float(adjustment.get("retained_weight", 1.0))
    return retained


def build_outcomes_in_e(derivation: Any) -> dict[str, Any] | None:
    """Return the outcome account for a behavioral derivation, or None.

    None when the observation is not behavioral, or when the outcome term was
    not part of it (fewer than three outcomes in the window).
    """
    if not isinstance(derivation, Mapping) or derivation.get("kind") != "behavioral_sensor":
        return None
    e_record = _e_record(derivation)
    component = _outcome_component(e_record) if e_record is not None else None
    if component is None:
        return None
    inputs = derivation.get("inputs")
    outcomes = inputs.get("outcomes") if isinstance(inputs, Mapping) else None
    if not isinstance(outcomes, list) or not outcomes:
        return None

    # The term's share of the final observation: its weight in the base E,
    # scaled by what the later blends keep of that base.
    retained = _retained_after_adjustments(e_record)
    share = float(component.get("weight", 0.0)) * retained
    counted = len(outcomes)
    adverse_rows = [o for o in outcomes if isinstance(o, Mapping) and o.get("is_bad")]
    adverse_by_type = Counter(str(o.get("outcome_type") or "unrecorded") for o in adverse_rows)
    by_source = Counter(
        str(o.get("verification_source") or "unrecorded")
        for o in outcomes
        if isinstance(o, Mapping)
    )
    return {
        "counted": counted,
        "adverse": len(adverse_rows),
        "adverse_by_type": dict(sorted(adverse_by_type.items())),
        "by_verification_source": dict(sorted(by_source.items())),
        "window": WINDOW_TEXT,
        "share_of_observation": round(share, 4),
        "term_contribution": round(float(component.get("weighted_contribution", 0.0)) * retained, 4),
        "term_range": [
            round(share * OUTCOME_E_FLOOR, 4),
            round(share * (OUTCOME_E_FLOOR + OUTCOME_E_SPAN), 4),
        ],
        "per_adverse_outcome": round(share * OUTCOME_E_SPAN / counted, 4),
        "caused_by_your_change": "not_recorded",
        "note": NOTE,
    }


def evidence_key(evidence: Mapping[str, Any]) -> tuple:
    """What has to change for the account to be news to the agent."""
    return (
        evidence.get("counted"),
        evidence.get("adverse"),
        tuple(sorted((evidence.get("adverse_by_type") or {}).items())),
    )


def mirror_line(evidence: Mapping[str, Any]) -> str:
    types = ", ".join(
        f"{name} x{count}" for name, count in (evidence.get("adverse_by_type") or {}).items()
    )
    low, high = evidence["term_range"]
    return (
        f"Outcomes in E: {evidence['adverse']} of {evidence['counted']} recorded "
        f"outcomes in the {evidence['window'].split(',')[0]} are adverse"
        f"{f' ({types})' if types else ''}; this term is "
        f"{evidence['share_of_observation']:.0%} of each E observation, now "
        f"{evidence['term_contribution']:.2f} on a {low:.2f}-{high:.2f} range. "
        "Counted as recorded, without attribution to your change; adverse means "
        "rework, not fault."
    )
