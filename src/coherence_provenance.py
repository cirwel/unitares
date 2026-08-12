"""Stable producer/role labels for the overloaded ``coherence`` field.

The public scalar remains for compatibility, but its producers are not
interchangeable.  Callers should carry both labels whenever they surface the
value so a controller output cannot silently masquerade as a health measure.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


LEGACY_COHERENCE_SOURCE = "legacy_tanh_v"
MANIFOLD_COHERENCE_SOURCE = "manifold"
BEHAVIORAL_COHERENCE_SOURCE = "behavioral_assessment"

ODE_CONTROL_FEEDBACK_ROLE = "ode_control_feedback"
EIS_STRUCTURAL_MEASUREMENT_ROLE = "eis_structural_measurement"
BEHAVIORAL_UPDATE_CONSISTENCY_ROLE = "behavioral_update_consistency"
UNKNOWN_COHERENCE_ROLE = "unknown"

_ROLE_BY_SOURCE = {
    LEGACY_COHERENCE_SOURCE: ODE_CONTROL_FEEDBACK_ROLE,
    MANIFOLD_COHERENCE_SOURCE: EIS_STRUCTURAL_MEASUREMENT_ROLE,
    "grounded": EIS_STRUCTURAL_MEASUREMENT_ROLE,
    BEHAVIORAL_COHERENCE_SOURCE: BEHAVIORAL_UPDATE_CONSISTENCY_ROLE,
}


def coherence_role_for_source(source: Any) -> str:
    """Return the semantic role attached to a producer label."""
    return _ROLE_BY_SOURCE.get(str(source or "").strip(), UNKNOWN_COHERENCE_ROLE)


def annotate_coherence_metrics(
    metrics: MutableMapping[str, Any],
    *,
    source: str,
) -> None:
    """Attach source and role to an existing coherence measurement."""
    if "coherence" not in metrics:
        return
    metrics["coherence_source"] = source
    metrics["coherence_role"] = coherence_role_for_source(source)
