"""Post-execution step: normalized agent-experience envelope.

Alias-gated: only calls invoked via an `experience=True` alias in
tool_stability (start_session, sync_state, check_working_state,
search_shared_memory, store_finding, update_finding, record_result,
request_review) get their response reshaped. Canonical tool names stay
byte-identical, so no existing client contract changes.

Envelope shape (friendly fields first, raw payload preserved):

    {
      "success": ...,
      "tool": "<friendly name as invoked>",
      "agent_uuid": ...,            # lifted when present
      "client_session_id": ...,     # lifted when present
      "action_summary": {...},      # action/reason/risk/evidence at a glance
      "next_action": ...,           # what to do next, concretely
      "state_summary": {...},       # compact working state
      "risk_summary": ...,          # plain-language risk read
      "legacy_diagnostics": {...},  # non-behavioral compatibility telemetry
      "memory_suggestions": [...],  # prior discoveries worth reading
      "response_options": {...},    # which response mode fits which task
      "_response_size": {...},      # approximate serialized size + reduction hint
      "recovery_hint": ...,         # only when state suggests trouble
      "raw_governance": {...}       # full canonical payload when requested
    }

Population is conservative: every field is harvested from values the
canonical handlers already return — this layer reorders and translates,
it does not compute new governance signals. Fields with nothing to say
are omitted. Default read aliases omit the repeated canonical payload and
advertise an explicit full-response escape hatch; other aliases retain it.
Error payloads (success=False / "error") pass through unchanged: the raw
error contract carries its own recovery info.

The step must never break a response: any parse/build failure returns
the original handler result untouched.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.mcp_handlers.response_formatter import (
    canonical_response_mode,
    normalize_discovery_list,
)
from src.mcp_handlers.support.param_normalization import (
    FRIENDLY_SEARCH_DETAIL_POLICY_KEY,
    FRIENDLY_SEARCH_DETAILS_REQUESTED_KEY,
)

logger = get_logger(__name__)

# Recovery threshold quoted to agents — the same risk ceiling used by the
# quick_resume contract. Legacy coherence is directional controller feedback
# and is deliberately absent from recovery guidance.
_RECOVERY_RISK_CEILING = 0.40

_MEMORY_SUGGESTION_LIMIT = 3

_ACTION_ALIASES = {
    "approve": ("proceed", None),
    "continue": ("proceed", None),
    "healthy": ("proceed", None),
    "ok": ("proceed", None),
    "safe": ("proceed", None),
    "caution": ("proceed", "guide"),
    "guide": ("proceed", "guide"),
    "block": ("pause", "block"),
    "high-risk": ("pause", "high-risk"),
    "reject": ("pause", "reject"),
    "stop": ("pause", "stop"),
}

_COMPACT_READ_ALIASES = frozenset({
    "check_working_state",
    "search_shared_memory",
})


def _lift(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Copy the named keys out of payload when present and non-None."""
    return {k: payload[k] for k in keys if payload.get(k) is not None}


def _harvest_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Use nested canonical payloads when a caller hands us an envelope."""
    raw = payload.get("raw_governance")
    return raw if isinstance(raw, dict) else payload


def _coherence_and_risk(payload: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Pull (coherence, risk_score) from the places handlers put them."""
    for container in (payload.get("metrics"), payload.get("current_state"), payload):
        if isinstance(container, dict):
            coherence = container.get("coherence")
            risk = container.get("risk_score")
            if coherence is not None or risk is not None:
                if isinstance(coherence, dict):
                    coherence = coherence.get("value")
                if isinstance(risk, dict):
                    risk = risk.get("value")
                try:
                    return (
                        float(coherence) if coherence is not None else None,
                        float(risk) if risk is not None else None,
                    )
                except (TypeError, ValueError):
                    return None, None
    return None, None


def _risk_summary(coherence: Optional[float], risk: Optional[float]) -> Optional[str]:
    if risk is None:
        return None
    if risk < _RECOVERY_RISK_CEILING:
        band = "low"
    elif risk < 0.7:
        band = "elevated"
    else:
        band = "high"
    parts = [f"risk {band} ({risk:.2f})"]
    if coherence is not None:
        parts.append(f"coherence diagnostic {coherence:.2f}")
    return ", ".join(parts)


def _verdict_value(payload: Dict[str, Any]) -> Optional[str]:
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    for verdict in (payload.get("verdict"), metrics.get("verdict")):
        if isinstance(verdict, dict):
            value = verdict.get("value") or verdict.get("action") or verdict.get("verdict")
        else:
            value = verdict
        if value is not None:
            return str(value).lower()
    return None


def _decision_action(payload: Dict[str, Any]) -> Optional[str]:
    decision = payload.get("decision")
    if decision is not None and not isinstance(decision, dict):
        return str(decision).lower()
    for container in (payload.get("decision"), payload.get("verdict"), payload):
        if not isinstance(container, dict):
            continue
        value = container.get("action") or container.get("value") or container.get("verdict")
        if value is not None:
            return str(value).lower()
    return None


def _verdict_evidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the first wrapped verdict-evidence block in a response.

    Compact check-ins put cold-start provenance under
    ``metrics.verdict.evidence``; read APIs may expose it under the top-level
    wrapped ``verdict``. The full response instead carries ``risk_attribution``.
    Reading both shapes is what keeps the action-first envelope honest in every
    response mode.
    """
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    for verdict in (payload.get("verdict"), metrics.get("verdict")):
        if not isinstance(verdict, dict):
            continue
        evidence = verdict.get("evidence")
        if isinstance(evidence, dict):
            return evidence
    return {}


def _verdict_assurance(payload: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Describe verdict maturity without inventing a confidence probability."""
    attribution = payload.get("risk_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    discriminability = attribution.get("discriminability")
    discriminability = discriminability if isinstance(discriminability, dict) else {}
    evidence = _verdict_evidence(payload)

    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    primary_source = metrics.get("primary_eisv_source") or payload.get(
        "primary_eisv_source"
    )
    driver = attribution.get("primary_driver")
    basis = evidence.get("basis") or driver or primary_source
    grade = str(evidence.get("grade") or "").strip().lower()
    cold_start = driver == "phi_cold_start" or primary_source in {
        "ode_fallback",
        "phi_cold_start",
    } or basis in {
        "ode_fallback",
        "phi_cold_start",
    }
    non_discriminative = discriminability.get("non_discriminative") is True
    if grade == "provisional" or cold_start or non_discriminative:
        return "provisional", str(basis or "non_discriminative_cold_start")

    if driver == "behavioral_assessment" or primary_source == "behavioral":
        return "non_provisional", str(driver or primary_source)
    if driver == "independent_verification_floor":
        return "non_provisional", str(driver)
    return "unspecified", str(basis) if basis is not None else None


def _one_line(value: Any, *, limit: int = 240) -> Optional[str]:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    if not compact:
        return None
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _action_summary(
    payload: Dict[str, Any],
    risk: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Build the small, stable block an agent can read before anything else."""
    decision = payload.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    policy = payload.get("policy_evaluation")
    policy = policy if isinstance(policy, dict) else {}
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}

    raw_action = decision.get("action") or _decision_action(payload)
    raw_action = str(raw_action).strip().lower() if raw_action is not None else None
    inferred_action, inferred_sub_action = _ACTION_ALIASES.get(
        raw_action,
        (raw_action, None),
    )
    sub_action = (
        decision.get("sub_action")
        or payload.get("sub_action")
        or policy.get("sub_action")
        or inferred_sub_action
    )

    verdict_obj = payload.get("verdict")
    if not isinstance(verdict_obj, dict):
        verdict_obj = metrics.get("verdict")
    verdict_obj = verdict_obj if isinstance(verdict_obj, dict) else {}
    reason = next(
        (
            text
            for text in (
                _one_line(decision.get("reason")),
                _one_line(decision.get("guidance")),
                _one_line(payload.get("reason")),
                _one_line(payload.get("guidance")),
                _one_line(policy.get("reason")),
                _one_line(policy.get("guidance")),
                _one_line(verdict_obj.get("meaning")),
                _one_line(payload.get("health_message")),
            )
            if text
        ),
        None,
    )
    verdict_confidence, evidence_basis = _verdict_assurance(payload)

    summary: Dict[str, Any] = {}
    if inferred_action:
        summary["action"] = inferred_action
    if sub_action:
        summary["sub_action"] = sub_action
    verdict = _verdict_value(payload)
    if verdict:
        summary["verdict"] = verdict
    if reason:
        summary["reason"] = reason
    if risk is not None:
        summary["risk_score"] = risk
    summary["verdict_confidence"] = verdict_confidence
    if evidence_basis:
        summary["evidence_basis"] = evidence_basis
    return summary or None


def _legacy_diagnostics(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Separate legacy ODE controller telemetry from behavioral verdict evidence."""
    metrics = payload.get("metrics")
    current_state = payload.get("current_state")
    for container in (
        metrics if isinstance(metrics, dict) else {},
        current_state if isinstance(current_state, dict) else {},
        payload,
    ):
        coherence = container.get("coherence")
        coherence_meta = coherence if isinstance(coherence, dict) else {}
        source = container.get("coherence_source") or container.get("source")
        role = container.get("coherence_role") or container.get("role")
        source = source or coherence_meta.get("source")
        role = role or coherence_meta.get("role")
        if source != "legacy_tanh_v" and role != "ode_control_feedback":
            continue
        if isinstance(coherence, dict):
            coherence = coherence.get("value")
        result: Dict[str, Any] = {
            "source": source or "legacy_tanh_v",
            "role": role or "ode_control_feedback",
            "health_evidence": False,
            "interpretation": (
                "Compatibility ODE controller feedback; diagnostic context, "
                "not a behavioral health score."
            ),
        }
        if coherence is not None:
            result["coherence"] = coherence
        return result
    return None


def _needs_attention(payload: Dict[str, Any]) -> bool:
    verdict = _verdict_value(payload)
    if verdict in {"guide", "pause", "reject"}:
        return True
    decision = payload.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    policy = payload.get("policy_evaluation")
    policy = policy if isinstance(policy, dict) else {}
    sub_action = (
        decision.get("sub_action")
        or payload.get("sub_action")
        or policy.get("sub_action")
    )
    if str(sub_action or "").lower() in {"guide", "reject", "pause"}:
        return True
    margin = str(payload.get("margin", "")).lower()
    return margin in {"tight", "boundary", "near_edge"}


def _compact_eisv(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    state = snapshot.get("primary_eisv") or snapshot.get("eisv")
    if not isinstance(state, dict):
        return None
    compact = _lift(state, "E", "I", "S", "V")
    source = snapshot.get("primary_eisv_source")
    if source is not None:
        compact["source"] = source
    return compact or None


def _recovery_hint(
    payload: Dict[str, Any],
    coherence: Optional[float],
    risk: Optional[float],
) -> Optional[str]:
    """Only speaks when the existing verdict/risk says something is off.

    The quick_resume/self_recovery thresholds are recovery-tool compatibility
    checks, not live-state alarms. The deployed coherence value is directional
    controller feedback, so it cannot independently produce a recovery warning.

    Every tool this names must be one the agent can actually call. The hints
    below said "self_recovery_review(...)" and "quick_resume()" until
    2026-08-29; both are register=False delegates, so the recovery_hint on a
    degraded check-in -- the field the onboarding text calls "the first
    recovery route" -- pointed at tool_not_found_error. The reachable spelling
    is self_recovery(action="quick"|"review"|"check"); the thresholds quoted
    here are unchanged (_RECOVERY_RISK_CEILING still mirrors
    QUICK_RESUME_MAX_RISK in lifecycle/self_recovery.py).
    """
    risky = risk is not None and risk >= _RECOVERY_RISK_CEILING
    attention = _needs_attention(payload)
    if not (risky or attention):
        return None
    action = _decision_action(payload) or _verdict_value(payload)
    severe = action in {"pause", "reject", "block", "stop"} or (
        risk is not None and risk >= 0.7
    )
    # Kept in the signature for wire/caller compatibility. Recovery guidance
    # follows the decision and measured risk; the overloaded coherence scalar
    # must not independently become a drift diagnosis.
    del coherence
    continuing = action in {"proceed", "continue", "approve", "ok", "healthy", "safe"}
    margin_hint = (
        "Policy margin is near an edge - keep scope tight, sync_state after "
        "the next substantial step, and use self_recovery(action='review', "
        "reflection='...') only if work stalls."
    )
    # _needs_attention fires on EITHER an advisory verdict (guide/pause/reject)
    # OR a near-edge margin, but both used to return the margin-worded hint. So
    # an agent with a comfortable margin and a `guide` verdict was told "policy
    # margin is near an edge" -- a threshold claim standing in for a verdict
    # condition, and unfalsifiable from the agent's side because no edge is
    # named. Say which one actually fired.
    verdict_hint = (
        "Verdict is advisory rather than a threshold warning - read the guidance "
        "text, sync_state after the next substantial step, and use "
        "self_recovery(action='review', reflection='...') only if work stalls."
    )
    margin_is_near_edge = str(payload.get("margin", "")).lower() in {
        "tight", "boundary", "near_edge"
    }
    if severe:
        return (
            "Working state looks degraded - pause and call "
            "self_recovery(action='review', reflection='...') before continuing."
        )
    if attention and continuing:
        return margin_hint if margin_is_near_edge else verdict_hint
    if risky:
        return (
            "Risk is elevated - if you feel stuck, self_recovery(action='quick') "
            f"applies when risk < {_RECOVERY_RISK_CEILING:.2f} and no void is "
            "active; otherwise self_recovery(action='review')."
        )
    # Attention-only reach (margin/verdict flag while measured risk sits below
    # the ceiling): never emit wording that contradicts the risk value the same
    # payload reports.
    #
    # This is the branch a `guide` verdict actually lands in: `continuing` is
    # False for "guide" (it is not in the proceed/approve/ok set), so the
    # attention+continuing case above is NOT reached. #1775 branched only that
    # earlier case and left this one returning the margin wording
    # unconditionally, which is why a comfortable margin still reported "policy
    # margin is near an edge" after that change deployed.
    return margin_hint if margin_is_near_edge else verdict_hint


def _verdict_caveat(source_payload: Dict[str, Any]) -> Optional[str]:
    """Plain-language warning that the verdict is provisional, lifted from
    risk_attribution to the envelope surface.

    The self-disclosure already exists in the canonical payload, but it sits
    three levels deep (risk_attribution.discriminability / .primary_driver).
    A reader skimming the friendly state_summary alone would see a clean
    'safe'/'proceed' and miss that the system is explicitly telling itself not
    to trust that verdict yet (dogfood: a maxed self-reported ethical_drift
    returned status=healthy with the only caveat buried in risk_attribution).
    This re-exposes it where the skimmer actually looks; it computes no new
    signal. Returns None when the verdict is NOT provisional (baseline warm).
    """
    verdict_confidence, evidence_basis = _verdict_assurance(source_payload)
    if verdict_confidence != "provisional":
        return None
    attribution = source_payload.get("risk_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    primary_driver = attribution.get("primary_driver")
    discriminability = attribution.get("discriminability")
    discriminability = discriminability if isinstance(discriminability, dict) else {}
    cold_start = primary_driver == "phi_cold_start"
    non_discriminative = discriminability.get("non_discriminative") is True
    until = discriminability.get("updates_until_baseline")
    tail = ""
    if isinstance(until, int) and until > 0:
        tail = f" ~{until} more check-in(s) until the behavioral signal is weighted."
    if cold_start or non_discriminative:
        evidence_path = "raw_governance.risk_attribution"
    elif isinstance(source_payload.get("verdict"), dict):
        evidence_path = "raw_governance.verdict.evidence"
    elif isinstance(source_payload.get("metrics"), dict) and isinstance(
        source_payload["metrics"].get("verdict"), dict
    ):
        evidence_path = "raw_governance.metrics.verdict.evidence"
    elif isinstance(source_payload.get("metrics"), dict) and source_payload[
        "metrics"
    ].get("primary_eisv_source") is not None:
        evidence_path = "raw_governance.metrics.primary_eisv_source"
    elif source_payload.get("primary_eisv_source") is not None:
        evidence_path = "raw_governance.primary_eisv_source"
    else:
        evidence_path = "raw_governance.metrics.verdict.evidence"
    return (
        "Verdict is provisional: the behavioral baseline is not warm. "
        "'safe'/'proceed' means no trouble detected under the cold-start prior, "
        f"not a validated all-clear. Evidence basis: {evidence_basis or 'cold-start prior'}. "
        f"See {evidence_path} for provenance." + tail
    )


def _reflection(source_payload: Dict[str, Any]) -> Optional[str]:
    """Lift the single mirror reflection to the envelope surface.

    The reflection ("You're near a basin boundary. Proceed carefully.") is a
    real actionable signal, but in the raw payload it lives only under
    _mirror_reflection (full mode) or reflection (mirror mode) — invisible to a
    reader of the friendly fields. Surface it as a top-level string when present.
    """
    for key in ("reflection", "_mirror_reflection", "_mirror_question"):
        value = source_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _memory_suggestions(payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Surface bounded discovery digests the canonical payload already carries.

    `relevant_prior_work` is the check-in path's contribution: the formatter
    already builds it from the mirror's KG lookup and puts it in the response,
    but nothing read it here, so `memory_suggestions` stayed empty on the one
    tool that had prior work to offer.

    `relevant_discoveries` arrives as {"message": ..., "discoveries": [...]}
    rather than a bare list, which the previous isinstance check discarded.
    """
    payload = _harvest_payload(payload)
    candidates = normalize_discovery_list(payload.get("relevant_discoveries"))
    if not candidates:
        candidates = (
            payload.get("relevant_prior_work")
            or payload.get("results")
            or payload.get("discoveries")
        )
    if not isinstance(candidates, list) or not candidates:
        return None
    suggestions = []
    for item in candidates[:_MEMORY_SUGGESTION_LIMIT]:
        if isinstance(item, dict):
            suggestions.append(
                _lift(
                    item,
                    "discovery_id",
                    "id",
                    "summary",
                    "title",
                    "type",
                    "status",
                    "tags",
                    "staleness_warning",
                    "similarity",
                    # the mirror path scores its hits as `relevance`; without it
                    # a suggestion arrives with no indication of match strength
                    "relevance",
                    "score",
                    "rrf_score",
                )
                or item
            )
        else:
            suggestions.append({"summary": str(item)})
    return suggestions or None


def _knowledge_write_summary(
    payload: Dict[str, Any],
    arguments: Optional[Dict[str, Any]],
) -> tuple[Optional[str], Dict[str, Any]]:
    """Lift the stable identity and result fields from a KG write response."""
    discovery = payload.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    arguments = arguments or {}
    discovery_id = (
        payload.get("discovery_id")
        or discovery.get("id")
        or arguments.get("discovery_id")
    )
    summary = _lift(
        discovery,
        "type",
        "status",
        "severity",
        "summary",
        "updated_at",
        "resolved_at",
    )
    if discovery_id is not None:
        summary["discovery_id"] = discovery_id
    message = payload.get("message")
    if message is not None:
        summary["message"] = message
    return str(discovery_id) if discovery_id is not None else None, summary


def _experience_aliases():
    """Return the live friendly-alias registry without creating an import cycle."""
    from ..tool_stability import list_all_aliases

    return {
        name: alias
        for name, alias in list_all_aliases().items()
        if alias.experience
    }


def _friendly_hint_text(value: str) -> str:
    """Translate canonical tool calls in a friendly hint to workflow aliases.

    Canonical payloads remain untouched under ``raw_governance``. This only
    rewrites the copied agent-facing hint, deriving names from the same alias
    registry that dispatch uses so response prose cannot drift independently.
    """
    aliases = _experience_aliases()
    result = value

    # Action-injecting aliases need the whole call prefix rewritten. A bare
    # ``knowledge``/``dialectic`` name is ambiguous and must stay canonical.
    for friendly_name, alias in aliases.items():
        if not alias.inject_action:
            continue
        canonical = re.escape(alias.new_name)
        action = re.escape(alias.inject_action)
        result = re.sub(
            rf"\b{canonical}\(\s*action\s*=\s*(['\"]){action}\1\s*,\s*",
            f"{friendly_name}(",
            result,
        )
        result = re.sub(
            rf"\b{canonical}\(\s*action\s*=\s*(['\"]){action}\1\s*\)",
            f"{friendly_name}()",
            result,
        )

    # Direct aliases have an unambiguous canonical-name replacement.
    direct = {
        alias.new_name: friendly_name
        for friendly_name, alias in aliases.items()
        if not alias.inject_action
    }
    for canonical, friendly_name in sorted(
        direct.items(), key=lambda item: len(item[0]), reverse=True
    ):
        result = re.sub(rf"\b{re.escape(canonical)}\b", friendly_name, result)
    return result


def _friendly_action_hint(value: Any) -> Any:
    """Recursively translate tool names in an agent-facing action hint."""
    if isinstance(value, str):
        return _friendly_hint_text(value)
    if isinstance(value, list):
        return [_friendly_action_hint(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_friendly_action_hint(item) for item in value)
    if not isinstance(value, dict):
        return value

    friendly = {
        key: _friendly_action_hint(item)
        for key, item in value.items()
    }
    tool = value.get("tool")
    action = value.get("action")
    if isinstance(tool, str):
        for friendly_name, alias in _experience_aliases().items():
            if alias.new_name != tool:
                continue
            if alias.inject_action is None or alias.inject_action == action:
                friendly["tool"] = friendly_name
                break
    return friendly


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _raw_governance_policy(
    friendly_name: str,
    arguments: Optional[Dict[str, Any]],
) -> tuple[bool, Optional[str]]:
    """Choose whether a friendly read alias should repeat its canonical payload.

    Canonical tools are unchanged. Read aliases default to their bounded
    experience envelope and retain an explicit full-response escape hatch.
    """
    if friendly_name not in _COMPACT_READ_ALIASES:
        return True, None

    arguments = arguments or {}
    if friendly_name == "check_working_state":
        verbosity = str(arguments.get("verbosity") or "").strip().lower()
        wants_full = (
            verbosity in {"standard", "full"}
            or not _as_bool(arguments.get("lite"), default=True)
            or _as_bool(arguments.get("include_state"), default=False)
        )
        return wants_full, (
            "Re-call check_working_state(lite=false) for the canonical diagnostics."
        )

    response_mode = str(
        arguments.get("response_mode") or "lean"
    ).strip().lower()
    return response_mode == "full", (
        "Re-call search_shared_memory(response_mode='full') for the complete result set."
    )


def _effective_discovery_retrieval_options(
    friendly_name: str,
    source_payload: Dict[str, Any],
    *,
    include_raw: bool,
    arguments: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Describe the discovery tier that actually survives the envelope.

    The canonical search can materialize full details before this middleware
    runs. A compact friendly response deliberately omits that raw payload and
    returns only bounded suggestions, so reporting ``full_inline`` there was a
    false claim: the caller paid the server-side work but received no details.
    """
    value = source_payload.get("discovery_retrieval_options")
    if not isinstance(value, dict):
        return None

    result = dict(value)
    result["all_inline"] = (
        "response_mode='full' plus include_details=true "
        "(can be large for multi-result searches)"
    )
    if friendly_name != "search_shared_memory" or include_raw:
        return result

    arguments = arguments or {}
    detail_policy = arguments.get(FRIENDLY_SEARCH_DETAIL_POLICY_KEY)
    if detail_policy == "digest_before_serialization":
        requested_details = bool(
            arguments.get(FRIENDLY_SEARCH_DETAILS_REQUESTED_KEY)
        )
        response_mode = str(
            arguments.get("response_mode") or "lean"
        ).strip().lower()
        result["detail_policy"] = detail_policy
        result["details_serialized"] = False
        result["details_included"] = False
        if requested_details:
            result["requested_tier"] = "full_inline"
            result["details_omitted_by"] = (
                f"response_mode='{response_mode}' before serialization"
            )
            result["note"] = (
                "Digest-mode friendly search keeps the upstream result bounded, "
                "even when include_details=true. Use response_mode='full' with "
                "include_details=true for every result inline, or open one record "
                "with knowledge(action='details', discovery_id='...')."
            )
        return result

    requested_tier = str(result.get("current_tier") or "digest")
    if requested_tier.startswith("full_inline"):
        response_mode = str(
            arguments.get("response_mode") or "lean"
        ).strip().lower()
        result["requested_tier"] = requested_tier
        result["current_tier"] = "digest"
        result["details_serialized"] = True
        result["details_included"] = False
        result["details_omitted_by"] = f"response_mode='{response_mode}'"
        result["note"] = (
            "The digest friendly response returns bounded previews even when "
            "include_details=true. Use response_mode='full' with "
            "include_details=true for every result inline, or open one record "
            "with knowledge(action='details', discovery_id='...')."
        )
    return result


def _response_options(
    friendly_name: str,
    payload: Dict[str, Any],
    arguments: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Put mode selection guidance beside the response it controls."""
    arguments = arguments or {}
    if friendly_name == "sync_state":
        current = payload.get("_mode") or canonical_response_mode(
            arguments.get("response_mode") or "auto"
        )
        return {
            "current": current,
            "routine": "compact",
            "interpreted_summary": "standard",
            "actionable_diagnostics": "mirror",
            "complete_audit": "full",
            "compatibility_aliases": (
                "lite=compact; verbose=full; interpreted=standard; "
                "minimal is the legacy bare shape"
            ),
        }
    if friendly_name == "search_shared_memory":
        current = str(arguments.get("response_mode") or "lean").strip().lower()
        return {
            "current": current,
            "digest": "lean",
            "diagnostic_digest": "compact",
            "complete_result_set": "full",
            "all_inline_details": "response_mode='full' + include_details=true",
        }
    if friendly_name == "check_working_state":
        current = "full" if not _as_bool(arguments.get("lite"), default=True) else "lite"
        return {
            "current": current,
            "routine": "lite=true",
            "complete_diagnostics": "lite=false",
        }
    return None


def _attach_response_size(
    envelope: Dict[str, Any],
    friendly_name: str,
) -> None:
    """Expose response cost before callers discover it through context pressure.

    The byte count intentionally excludes this metadata field, avoiding a
    self-referential size calculation while staying within a few dozen bytes of
    the final serialized payload.
    """
    measured_bytes = len(
        json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    )
    size_class = (
        "small" if measured_bytes < 4_000
        else "medium" if measured_bytes < 12_000
        else "large"
    )
    metadata: Dict[str, Any] = {
        "approx_bytes": measured_bytes,
        "approx_kb": round(measured_bytes / 1_000, 1),
        "size_class": size_class,
        "measured_without_self": True,
    }
    if measured_bytes >= 4_000:
        current = envelope.get("response_options", {}).get("current")
        if friendly_name == "search_shared_memory":
            metadata["reduce_with"] = (
                "Use include_details=false and response_mode='lean'; open one "
                "discovery with knowledge(action='details', discovery_id='...')."
            )
        elif friendly_name == "sync_state" and current == "full":
            metadata["reduce_with"] = (
                "Use response_mode='compact' for routine check-ins or 'mirror' "
                "for actionable diagnostics."
            )
        elif friendly_name == "sync_state":
            metadata["reduce_with"] = (
                "Use response_mode='minimal' only when the bare action/EISV "
                "snapshot is sufficient."
            )
        elif friendly_name == "start_session":
            metadata["reduce_with"] = "Use response_mode='minimal'."
        elif friendly_name == "check_working_state":
            metadata["reduce_with"] = "Use lite=true."
    envelope["_response_size"] = metadata


def build_experience_envelope(
    friendly_name: str,
    canonical_name: str,
    payload: Dict[str, Any],
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Reshape a successful canonical payload into the experience envelope.

    Pure function over the parsed payload — raises nothing the caller
    can't recover from (callers guard anyway).
    """
    envelope: Dict[str, Any] = {
        "success": payload.get("success", True),
        "tool": friendly_name,
    }
    envelope.update(_lift(payload, "agent_uuid", "client_session_id"))
    if "agent_uuid" not in envelope and payload.get("uuid") is not None:
        envelope["agent_uuid"] = payload["uuid"]

    source_payload = _harvest_payload(payload)
    coherence, risk = _coherence_and_risk(source_payload)
    include_raw, raw_hint = _raw_governance_policy(friendly_name, arguments)
    retrieval_options = _effective_discovery_retrieval_options(
        friendly_name,
        source_payload,
        include_raw=include_raw,
        arguments=arguments,
    )

    if canonical_name in {"process_agent_update", "get_governance_metrics"}:
        summary = _action_summary(source_payload, risk)
        if summary:
            envelope["action_summary"] = summary
        legacy = _legacy_diagnostics(source_payload)
        if legacy:
            envelope["legacy_diagnostics"] = legacy

    options = _response_options(friendly_name, source_payload, arguments)
    if options:
        envelope["response_options"] = options

    next_action: Any = None
    state_summary: Optional[Dict[str, Any]] = None
    is_knowledge_search = False

    if canonical_name == "onboard":
        next_action = (
            "Save agent_uuid and client_session_id, then check in with "
            "sync_state(response_text='...', complexity=0.5, "
            "client_session_id=...) as you work."
        )
        state_summary = _lift(
            payload,
            "lineage_state",
            "session_key",
            "onboard_origin",
            "onboard_origin_basis",
        )
        predecessor = (
            payload.get("thread_context", {}).get("predecessor", {})
            if isinstance(payload.get("thread_context"), dict)
            else {}
        )
        if isinstance(predecessor, dict) and predecessor.get("uuid"):
            state_summary["predecessor_uuid"] = predecessor["uuid"]
            next_action += (
                " A predecessor was detected - declare its uuid as "
                "parent_agent_id on your NEXT fresh start_session, not now."
            )

    elif canonical_name == "process_agent_update":
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        state_summary = _lift(payload, "status", "health_status")
        # margin_scope and unmeasurable_edges ride WITH margin: a bare
        # "comfortable" would otherwise read as "no limit is near" when an edge
        # was never assessed at all. Lifting them here is what makes the
        # unmeasurable state agent-visible rather than a comment in `details`,
        # which this envelope strips.
        state_summary.update(_lift(decision, "action", "margin", "nearest_edge",
                                   "margin_scope", "unmeasurable_edges"))
        for key, value in _lift(
            payload,
            "action",
            "sub_action",
            "margin",
            "nearest_edge",
            "margin_scope",
            "unmeasurable_edges",
        ).items():
            state_summary.setdefault(key, value)
        if coherence is not None:
            if legacy:
                # Match get_governance_metrics's inline badge (runtime_queries.py's
                # lite branch) instead of a bare float agents have to cross-reference
                # against the separate legacy_diagnostics block to interpret.
                state_summary["coherence"] = {
                    "value": coherence,
                    "status": "⚪ legacy control feedback (not health-rated)",
                    "source": legacy.get("source", "legacy_tanh_v"),
                    "role": legacy.get("role", "ode_control_feedback"),
                }
            else:
                state_summary["coherence"] = coherence
        if risk is not None:
            state_summary["risk_score"] = risk
        prediction_id = payload.get("prediction_id") or source_payload.get("prediction_id")
        if isinstance(prediction_id, str) and prediction_id:
            # The id already sits in the canonical payload; naming it here is
            # what makes registry-bound record_result discoverable — otherwise
            # the outcome grades a confidence borrowed from an unrelated
            # earlier turn (fallback binding dominates calibration rows).
            next_action = (
                "Keep working - sync_state again after your next substantial "
                "step. When an outcome lands, record_result(outcome_type=..., "
                f"prediction_id='{prediction_id}') so it grades this check-in's "
                "confidence rather than a fallback."
            )
        else:
            next_action = (
                "Keep working - sync_state again after your next substantial step, "
                "and record_result(...) when an outcome lands."
            )
        # In-flow review nudge (#1685): the formatter attaches review_suggested
        # (every response mode) when this check-in reported uncertain ground.
        nudge = payload.get("review_suggested")
        if isinstance(nudge, dict):
            phrase = {
                "low_confidence": "low confidence",
                "high_complexity": "high complexity",
                "guide_verdict": "a guide verdict",
            }.get(str(nudge.get("trigger")), "uncertain ground")
            next_action += (
                f" This check-in carried {phrase} - a reviewer can pressure-test "
                "it in one call: request_review(issue_description='...', "
                "reasoning='...')."
            )

    elif canonical_name == "get_governance_metrics":
        # Preserve the essential EISV read at the friendly surface so compact
        # mode can omit the repeated canonical payload without making the state
        # tool useless.
        next_action = payload.get("next_action") or payload.get("guidance")
        verdict = payload.get("verdict")
        if verdict is not None:
            state_summary = (
                dict(verdict) if isinstance(verdict, dict) else {"verdict": verdict}
            )
        else:
            state_summary = {}
        for key, value in _lift(
            payload,
            "status",
            "primary_eisv_source",
            "E",
            "I",
            "S",
            "V",
            "coherence",
            "risk_score",
        ).items():
            state_summary.setdefault(key, value)

    elif canonical_name == "knowledge" and friendly_name in {
        "store_finding",
        "update_finding",
    }:
        discovery_id, state_summary = _knowledge_write_summary(
            source_payload,
            arguments,
        )
        message = source_payload.get("message")
        if message is not None:
            envelope["message"] = message
        if discovery_id is not None:
            envelope["discovery_id"] = discovery_id

        if friendly_name == "store_finding":
            next_action = source_payload.get("_resolve_when_done")
            if not next_action:
                suffix = (
                    f"discovery_id='{discovery_id}'"
                    if discovery_id is not None
                    else "discovery_id='...'"
                )
                next_action = (
                    "Finding stored. When it is addressed, close the loop with "
                    f"update_finding({suffix}, status='resolved', "
                    "resolution_notes='...')."
                )
        else:
            status = state_summary.get("status")
            target = discovery_id or "..."
            if status in {
                "resolved",
                "closed",
                "wont_fix",
                "archived",
                "superseded",
                "cold",
            }:
                next_action = (
                    f"Finding '{target}' is now '{status}'. Read the final record "
                    "with knowledge(action='details', "
                    f"discovery_id='{target}')."
                )
            else:
                next_action = (
                    f"Finding '{target}' was updated. Read it with "
                    "knowledge(action='details', "
                    f"discovery_id='{target}'); when addressed, close it with "
                    f"update_finding(discovery_id='{target}', status='resolved', "
                    "resolution_notes='...')."
                )

    elif canonical_name == "knowledge":
        is_knowledge_search = True
        candidates = source_payload.get("results") or source_payload.get("discoveries") or []
        total = source_payload.get("total_count")
        if total is None:
            total = source_payload.get("count")
        if total is None:
            total = len(candidates)
        low_confidence = bool(source_payload.get("low_confidence"))
        if low_confidence:
            next_action = (
                f"{total} exploratory low-confidence discoveries surfaced. "
                "Treat them as possible leads, not authoritative matches; "
                "rephrase with distinctive terms or open details before using them."
            )
        elif total:
            next_action = (
                f"{total} prior discoveries matched - read before redoing work. "
                "Full context: knowledge(action='details', discovery_id=...). "
                "Record new findings: knowledge(action='store', summary='...')."
            )
        else:
            next_action = (
                "No prior discoveries matched. Broaden terms or search tags before "
                "recording a new finding."
            )
        state_summary = _lift(
            source_payload,
            "count",
            "total_count",
            "search_mode_used",
            "search_mode_requested",
            "operator_used",
            "low_confidence",
            "search_degraded",
            "tag_filter_dropped",
        )
        note = source_payload.get("confidence_note")
        if note:
            state_summary["confidence_note"] = note
        if retrieval_options and retrieval_options.get("current_tier"):
            state_summary["result_tier"] = retrieval_options["current_tier"]
        state_summary["results_shown_in_digest"] = min(
            len(candidates),
            _MEMORY_SUGGESTION_LIMIT,
        )

    elif canonical_name == "outcome_event":
        state_summary = _lift(
            payload,
            "outcome_id",
            "outcome_type",
            "outcome_score",
            "recorded_at",
            "corroboration_grade",
            "evidence_weight",
            "claim_risk",
        )
        snapshot = payload.get("eisv_snapshot")
        if isinstance(snapshot, dict):
            compact = _compact_eisv(snapshot)
            if compact:
                state_summary["working_state"] = compact
        next_action = "Outcome recorded - continue, or sync_state to fold it into your working state."

    elif canonical_name == "dialectic":
        state_summary = _lift(
            payload,
            "session_id",
            "phase",
            "reviewer",
            "reviewer_agent_id",
            "whose_move",
            "one_call_review",
            "thesis_source",
            "review_verdict",
        )
        session_id = state_summary.get("session_id", "...")
        next_action = payload.get("next_call") or payload.get("next_step")
        if not next_action:
            whose_move = str(payload.get("whose_move") or "").strip()
            phase = str(payload.get("phase") or "").lower()
            if whose_move:
                next_action = whose_move
            elif phase in {"resolved", "failed", "escalated"}:
                verdict = payload.get("review_verdict") or phase
                next_action = (
                    f"Review session {session_id} is {phase} ({verdict}); follow "
                    "the recorded verdict or resolution."
                )
            else:
                next_action = (
                    "Review session open - advance it without copying the saved "
                    f"brief: dialectic(action='thesis', session_id='{session_id}', "
                    "use_brief_as_thesis=true)."
                )

    if next_action is not None:
        envelope["next_action"] = _friendly_action_hint(next_action)
    if state_summary:
        # state_summary can carry glossary coaching (e.g. an uninitialized
        # verdict's "Submit one process_agent_update...") — translate it like
        # next_action so canonical names don't leak through the friendly
        # surface (dogfood 2026-08-20 register-mismatch report).
        envelope["state_summary"] = _friendly_action_hint(state_summary)

    risk_text = _risk_summary(coherence, risk)
    if risk_text:
        envelope["risk_summary"] = risk_text

    # Provisional-verdict caveat: pull the cold-start / non-discriminative
    # self-disclosure out of risk_attribution so a reader of state_summary
    # alone is not misled by a clean 'safe'/'proceed'. Also flag it inside
    # state_summary itself, where the skimmer actually looks.
    caveat = _verdict_caveat(source_payload)
    if caveat:
        envelope["verdict_caveat"] = caveat
        summary = envelope.get("state_summary")
        if isinstance(summary, dict):
            summary["verdict_provisional"] = True

    reflection = _reflection(source_payload)
    if reflection:
        envelope["reflection"] = reflection

    suggestions_enabled = (
        canonical_name != "process_agent_update"
        or _as_bool((arguments or {}).get("include_memory_suggestions"), default=False)
    )
    suggestions = _memory_suggestions(payload) if suggestions_enabled else None
    # When the full canonical discoveries list is about to go out under
    # raw_governance, a trimmed top-N copy under memory_suggestions is a
    # strict subset of it in the same response — drop the duplicate rather
    # than serialize the same results twice.
    if suggestions and not (is_knowledge_search and include_raw):
        envelope["memory_suggestions"] = suggestions
    if retrieval_options:
        envelope["discovery_retrieval_options"] = _friendly_action_hint(
            retrieval_options
        )
    for key in (
        "low_confidence",
        "confidence_note",
        "search_degraded",
        "search_degraded_message",
    ):
        value = source_payload.get(key)
        if value is not None:
            envelope[key] = value

    hint = _recovery_hint(source_payload, coherence, risk)
    if hint:
        envelope["recovery_hint"] = hint

    if include_raw:
        envelope["raw_governance"] = payload
    else:
        envelope["raw_governance_available"] = True
        if raw_hint:
            envelope["raw_governance_hint"] = raw_hint
    _attach_response_size(envelope, friendly_name)
    return envelope


async def apply_experience_envelope(name: str, arguments: Dict[str, Any], ctx, result):
    """POST_EXECUTION step. `name` is the canonical (post-alias) tool;
    the invoked name lives in ctx.original_name. Returns the (possibly
    reshaped) handler result; on ANY failure returns it untouched."""
    try:
        from ..tool_stability import is_experience_alias

        invoked = getattr(ctx, "original_name", None)
        if not invoked or not is_experience_alias(invoked):
            return result

        if not (isinstance(result, (list, tuple)) and result and hasattr(result[0], "text")):
            return result
        payload = json.loads(result[0].text)
        if not isinstance(payload, dict):
            return result
        if payload.get("success") is False or "error" in payload:
            return result  # raw error contract carries its own recovery info

        envelope = build_experience_envelope(invoked, name, payload, arguments)
        return [TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))]
    except Exception:
        logger.warning(
            "experience envelope failed for %r - returning raw response",
            getattr(ctx, "original_name", name),
            exc_info=True,
        )
        return result
