"""Resolve CheckinResult fields from either shape ``sync_state`` returns.

``sync_state`` is an experience alias of ``process_agent_update``, so its
response is the envelope built by the server's ``envelope_step``: the verdict
sits at ``state_summary.action``, and the canonical payload rides under
``raw_governance`` only when the caller asked for ``response_mode="full"``.
Until #2366 both SDK clients read the canonical top-level ``decision`` /
``metrics`` keys — which the default compact envelope never carries — so every
check-in parsed as ``"proceed"`` with ``coherence``/``risk`` ``None``, and the
pause/reject branches in ``agent.py`` could never fire.

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
    ``None`` when absent. ``metrics`` is the canonical metrics dict wherever
    it lives, for callers that forward it (substrate emission).
    """
    decision = _dict(raw.get("decision"))
    metrics = _dict(raw.get("metrics"))
    governance = _dict(raw.get("raw_governance"))
    gov_decision = _dict(governance.get("decision"))
    gov_metrics = _dict(governance.get("metrics"))
    summary = _dict(raw.get("state_summary"))

    verdict = _first(
        decision.get("action"),
        gov_decision.get("action"),
        summary.get("action"),
        _scalar(raw.get("verdict")),
        _scalar(governance.get("verdict")),
    )
    guidance = _first(
        decision.get("guidance"),
        raw.get("guidance"),
        gov_decision.get("guidance"),
        governance.get("guidance"),
        # The compact envelope lifts no guidance; next_action is the concrete
        # instruction it carries instead (the pause text names self_recovery).
        raw.get("next_action"),
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
