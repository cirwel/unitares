"""Resolve MetricsResult fields from either shape ``check_working_state`` returns.

``get_metrics`` calls ``check_working_state``, an experience alias of
``get_governance_metrics``, so the response is the envelope built by the
server's ``envelope_step``: the reading sits in ``state_summary`` (``E``,
``I``, ``S``, ``V``, ``coherence``, ``risk_score``), the verdict and policy
action in ``action_summary``, and the canonical payload rides under
``raw_governance`` only when the caller asks for ``verbosity="standard"`` or
``"full"``. Neither shape has a top-level ``metrics`` key, which is the only
key ``MetricsResult`` read, so ``MetricsResult.metrics`` came back ``{}`` on
every call: the same drift #2366 fixed for check-ins.

Precedence follows :mod:`unitares_sdk._checkin_fields`: canonical first (a
direct ``get_governance_metrics`` caller's top level, then the
``raw_governance`` copy), then the envelope's summaries. The default minimal
tier badges each value (``{"value": ..., "label": ...}``), and the envelope
badges a legacy coherence the same way; both resolve to their ``value``.
"""

from __future__ import annotations

from typing import Any

from unitares_sdk._checkin_fields import _dict, _first, _scalar

# The reading, as the canonical payload names it. primary_eisv_source says
# whether E/I/S/V were measured ("behavioral") or are the controller's
# fallback ("ode_fallback" before the first check-in), so it travels with them.
READING_KEYS = (
    "E",
    "I",
    "S",
    "V",
    "coherence",
    "risk_score",
    "verdict",
    "primary_eisv_source",
)


def resolve_metrics_fields(raw: dict) -> dict[str, Any]:
    """Return ``metrics``, ``verdict``, ``action``, ``coherence`` and ``risk``.

    ``metrics`` holds whichever of :data:`READING_KEYS` the response carries,
    as scalars. A response that already carries a non-empty top-level
    ``metrics`` dict keeps it unchanged. ``verdict`` is the assessment
    (``safe``, ``caution``, ``high-risk``, or ``uninitialized`` / ``unbound``
    when there is no reading to assess). ``action`` is the policy action,
    ``proceed`` or ``pause``; with no decision to report, the envelope's
    ``action_summary.action`` repeats ``uninitialized`` (no check-in yet) or
    ``unbound`` (no identity on the call), and so does this. Absent values
    are ``None``: an unbound read carries no reading, so its ``metrics`` holds
    only ``verdict="unbound"``.
    """
    governance = _dict(raw.get("raw_governance"))
    summary = _dict(raw.get("state_summary"))
    action_summary = _dict(raw.get("action_summary"))

    reading: dict[str, Any] = {}
    for key in READING_KEYS:
        value = _first(
            _scalar(raw.get(key)),
            _scalar(governance.get(key)),
            action_summary.get(key) if key in ("verdict", "risk_score") else None,
            _scalar(summary.get(key)),
        )
        if value is not None:
            reading[key] = value

    action = _first(
        _dict(raw.get("verdict")).get("decision_action"),
        raw.get("last_decision_action"),
        _dict(governance.get("verdict")).get("decision_action"),
        governance.get("last_decision_action"),
        action_summary.get("action"),
        summary.get("decision_action"),
    )

    # No server shape carries a top-level metrics dict, but a caller's own
    # fixture or proxy may; it is kept, and read where the reading is silent.
    explicit = _dict(raw.get("metrics"))
    return {
        "metrics": explicit or reading,
        "verdict": _first(reading.get("verdict"), _scalar(explicit.get("verdict"))),
        "action": action,
        "coherence": _first(
            reading.get("coherence"), _scalar(explicit.get("coherence"))
        ),
        "risk": _first(
            reading.get("risk_score"),
            _scalar(explicit.get("risk_score")),
            _scalar(explicit.get("risk")),
        ),
    }
