"""Agent-facing account of which behavioral axes were measured this check-in.

Each of E, I and S is built from one or more base terms. When none of an
axis's base terms was supplied, the sensor writes a mid-range default in its
place, so the published number looks the same as a reading. Nothing in the
check-in response said which was which: an agent could not tell a measured E
from a filled-in one, and two agents with no inputs looked identical.

This module reads the derivation the check-in already built
(``eisv_telemetry.build_behavioral_derivation``) and restates, per axis,
whether any base term was observed. It computes no new signal and changes no
input: the flags come from the same component record the formula produced, so
the account cannot drift from what was scored. Measurement-only; nothing in
the policy path reads it.

An axis counts as measured when at least one of its base terms is flagged
``observed``. The later blend adjustments are not enough on their own: they
adjust a reading, and with a defaulted base there is no reading to adjust. V is
derived from E and I, so it is reported as resting on defaults whenever either
of them does.

The account is a lower bound on emptiness. A term flagged observed can still
sit at a clip bound or arrive as the same constant on every check-in, which a
single check-in cannot show.
"""

from __future__ import annotations

from typing import Any, Mapping

AXES = ("E", "I", "S")

# What each base term reads, stated as mechanism. Keyed by the component names
# ``behavioral_sensor`` writes; an unlisted name is reported without a gloss.
TERM_READS = {
    "complexity_calibration": (
        "the gap between the complexity you report and the complexity "
        "estimated from your report text"
    ),
    "outcome_success": "the share of your recent recorded outcomes that were not adverse",
    "calibration_accuracy": "your recent confidence-and-outcome pairs",
    "outcome_score_consistency": "the spread of at least three recent outcome scores",
    "uncalibrated_base_fallback": (
        "your recent confidence-and-outcome pairs, or the spread of at least "
        "three recent outcome scores"
    ),
    "drift_norm": "the drift reading for this check-in",
    "complexity_divergence": (
        "the gap between the complexity you report and the complexity "
        "estimated from your report text"
    ),
}

NOTE = (
    "A defaulted axis is a placeholder, not a low or high reading. It says "
    "nothing about how the work went."
)


def build_reading_coverage(derivation: Any) -> dict[str, Any] | None:
    """Return which axes were measured for a behavioral derivation, or None.

    None when the observation is not behavioral or carries no component record
    (fewer than three history entries), since there is then no per-term record
    to restate.
    """
    if not isinstance(derivation, Mapping) or derivation.get("kind") != "behavioral_sensor":
        return None
    components = derivation.get("components")
    dimensions = components.get("dimensions") if isinstance(components, Mapping) else None
    if not isinstance(dimensions, Mapping):
        return None

    measured: list[str] = []
    defaulted: dict[str, list[str]] = {}
    for axis in AXES:
        record = dimensions.get(axis)
        base_terms = record.get("components") if isinstance(record, Mapping) else None
        if not isinstance(base_terms, list) or not base_terms:
            return None
        if any(isinstance(term, Mapping) and term.get("observed") is True for term in base_terms):
            measured.append(axis)
        else:
            defaulted[axis] = [
                str(term.get("name")) for term in base_terms if isinstance(term, Mapping)
            ]

    share = derivation.get("unobserved_share")
    by_dimension = share.get("by_dimension") if isinstance(share, Mapping) else None
    return {
        "measured": measured,
        "defaulted": defaulted,
        "v_rests_on_defaults": "E" in defaulted or "I" in defaulted,
        "unobserved_share": (
            {axis: round(float(by_dimension[axis]), 4) for axis in AXES}
            if isinstance(by_dimension, Mapping)
            and all(isinstance(by_dimension.get(axis), (int, float)) for axis in AXES)
            else None
        ),
        "note": NOTE,
    }


def evidence_key(coverage: Mapping[str, Any]) -> tuple:
    """What has to change for the account to be news to the agent."""
    defaulted = coverage.get("defaulted") or {}
    return tuple(sorted((axis, tuple(terms)) for axis, terms in defaulted.items()))


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def mirror_line(coverage: Mapping[str, Any]) -> str:
    defaulted = coverage.get("defaulted") or {}
    measured = list(coverage.get("measured") or [])
    axes = [axis for axis in AXES if axis in defaulted]
    if not axes:
        return f"Reading coverage: {_join(measured)} were all measured in this check-in."
    verb = "is a default" if len(axes) == 1 else "are defaults"
    parts = [f"Reading coverage: {_join(axes)} in this check-in {verb}, not a reading."]
    for axis in axes:
        # Any one supplied base term makes the axis a reading, so every
        # distinct thing the axis can read is named, joined by "or".
        reads = list(dict.fromkeys(
            TERM_READS[name] for name in defaulted[axis] if name in TERM_READS
        ))
        if reads:
            missing = "neither was supplied" if len(reads) == 2 else "none was supplied"
            parts.append(f"{axis} reads {', or '.join(reads)}; {missing}.")
    if measured:
        parts.append(f"{_join(measured)} {'was' if len(measured) == 1 else 'were'} measured.")
    if coverage.get("v_rests_on_defaults"):
        parts.append("V is derived from E and I, so it is not a reading here either.")
    return " ".join(parts)
