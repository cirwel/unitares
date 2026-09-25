"""Resolve CheckinResult fields from either shape ``sync_state`` returns.

``sync_state`` is an experience alias of ``process_agent_update``, so its
response is the envelope built by the server's ``envelope_step``: the verdict
sits at ``state_summary.action``, and the canonical payload rides under
``raw_governance`` only when the caller asked for ``response_mode="full"``.
Until #2366 both SDK clients read the canonical top-level ``decision`` /
``metrics`` keys — which the default compact envelope never carries — so every
check-in parsed as ``"proceed"`` with ``coherence``/``risk`` ``None``, and the
pause branch in ``agent.py`` could never fire. (Its ``reject`` branch is a
separate matter: the server's ``decision.action`` is only ``proceed`` or
``pause``; ``guide`` and ``reject`` exist as ``sub_action`` only, so this
module does not make that branch reachable.)

Both clients resolve through :func:`resolve_checkin_fields` so the two
parsers cannot drift apart again. Precedence is canonical first (a direct
``process_agent_update`` caller, or the ``raw_governance`` copy), then the
envelope's ``state_summary`` lift.
"""

from __future__ import annotations

from typing import Any


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _scalar(value: Any) -> Any:
    # envelope_step badges a legacy tanh coherence inline as
    # {"value": ..., "status": ..., "source": ..., "role": ...}; the mirror
    # payload wraps the verdict the same way ({"value": "pause"}).
    if isinstance(value, dict):
        return value.get("value")
    return value


def resolve_checkin_fields(raw: dict) -> dict[str, Any]:
    """Return ``verdict``, ``guidance``, ``coherence``, ``risk`` and ``metrics``.

    ``verdict`` always resolves to a string (``"proceed"`` when no shape
    carries an action, matching the historical default); the others are
    ``None`` when absent. ``verdict`` is the binary policy action; a guide
    is ``"proceed"`` with the server's reason as ``guidance``.

    ``metrics`` is the canonical metrics dict when the response carries one
    (a direct ``process_agent_update`` caller, or ``response_mode="full"``).
    The default compact envelope carries none, so it is ``{}`` there and
    callers that forward it for substrate emission skip on that path.
    """
    decision = _dict(raw.get("decision"))
    metrics = _dict(raw.get("metrics"))
    governance = _dict(raw.get("raw_governance"))
    gov_decision = _dict(governance.get("decision"))
    gov_metrics = _dict(governance.get("metrics"))
    summary = _dict(raw.get("state_summary"))
    action_summary = _dict(raw.get("action_summary"))

    verdict = _first(
        decision.get("action"),
        gov_decision.get("action"),
        summary.get("action"),
        _scalar(raw.get("verdict")),
        _scalar(governance.get("verdict")),
    )
    sub_action = _first(
        decision.get("sub_action"),
        gov_decision.get("sub_action"),
        summary.get("sub_action"),
        action_summary.get("sub_action"),
    )
    # The compact envelope lifts no guidance. What stands in for it depends
    # on the decision: a pause's next_action is the concrete instruction (it
    # names the recovery route: self_recovery below the review gate, the
    # dialectic session above it); a guide's is the server's reason. On any other
    # proceed, next_action is a generic "keep working" prompt, not guidance.
    if verdict == "pause":
        envelope_guidance = _first(raw.get("next_action"), action_summary.get("reason"))
    elif sub_action == "guide":
        envelope_guidance = action_summary.get("reason")
    else:
        envelope_guidance = None
    guidance = _first(
        decision.get("guidance"),
        raw.get("guidance"),
        gov_decision.get("guidance"),
        governance.get("guidance"),
        envelope_guidance,
    )
    canonical_metrics = metrics or gov_metrics
    coherence = _first(
        metrics.get("coherence"),
        gov_metrics.get("coherence"),
        _scalar(summary.get("coherence")),
    )
    risk = _first(
        metrics.get("risk_score"),
        metrics.get("risk"),
        gov_metrics.get("risk_score"),
        summary.get("risk_score"),
    )
    return {
        "verdict": str(verdict) if verdict is not None else "proceed",
        "guidance": guidance or None,
        "coherence": coherence,
        "risk": risk,
        "metrics": canonical_metrics,
    }
