"""Post-execution step: normalized agent-experience envelope.

Alias-gated: only calls invoked via an `experience=True` alias in
tool_stability (start_session, sync_state, check_working_state,
search_shared_memory, record_result, request_review) get their
response reshaped. Canonical tool names stay byte-identical, so no
existing client contract changes.

Envelope shape (friendly fields first, raw payload preserved):

    {
      "success": ...,
      "tool": "<friendly name as invoked>",
      "agent_uuid": ...,            # lifted when present
      "client_session_id": ...,     # lifted when present
      "next_action": ...,           # what to do next, concretely
      "state_summary": {...},       # compact working state
      "risk_summary": ...,          # plain-language risk read
      "memory_suggestions": [...],  # prior discoveries worth reading
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

logger = get_logger(__name__)

# Recovery threshold quoted to agents — the same risk ceiling used by the
# quick_resume contract. Legacy coherence is directional controller feedback
# and is deliberately absent from recovery guidance.
_RECOVERY_RISK_CEILING = 0.40

_MEMORY_SUGGESTION_LIMIT = 3

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
    verdict = payload.get("verdict")
    if isinstance(verdict, dict):
        value = verdict.get("value") or verdict.get("action") or verdict.get("verdict")
    else:
        value = verdict
    return str(value).lower() if value is not None else None


def _decision_action(payload: Dict[str, Any]) -> Optional[str]:
    for container in (payload.get("decision"), payload.get("verdict"), payload):
        if not isinstance(container, dict):
            continue
        value = container.get("action") or container.get("value") or container.get("verdict")
        if value is not None:
            return str(value).lower()
    return None


def _needs_attention(payload: Dict[str, Any]) -> bool:
    verdict = _verdict_value(payload)
    if verdict in {"guide", "pause", "reject"}:
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
        "the next substantial step, and use self_recovery_review(reflection='...') "
        "only if work stalls."
    )
    if severe:
        return (
            "Working state looks degraded - pause and call "
            "self_recovery_review(reflection='...') before continuing."
        )
    if attention and continuing:
        return margin_hint
    if risky:
        return (
            "Risk is elevated - if you feel stuck, quick_resume() applies when "
            f"risk < {_RECOVERY_RISK_CEILING:.2f} and no void is active; otherwise "
            "self_recovery_review()."
        )
    # Attention-only reach (margin/verdict flag while measured risk sits below
    # the ceiling): never emit wording that contradicts the risk value the same
    # payload reports.
    return margin_hint


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
    attribution = source_payload.get("risk_attribution")
    if not isinstance(attribution, dict):
        return None
    primary_driver = attribution.get("primary_driver")
    discriminability = attribution.get("discriminability")
    discriminability = discriminability if isinstance(discriminability, dict) else {}
    cold_start = primary_driver == "phi_cold_start"
    non_discriminative = discriminability.get("non_discriminative") is True
    if not (cold_start or non_discriminative):
        return None
    until = discriminability.get("updates_until_baseline")
    tail = ""
    if isinstance(until, int) and until > 0:
        tail = f" ~{until} more check-in(s) until the behavioral signal is weighted."
    return (
        "Verdict is provisional: the behavioral baseline isn't warm yet, so it "
        "runs on the cold-start prior and risk_score is non-discriminative "
        "during bootstrap. A 'safe'/'proceed' here means 'no evidence of trouble "
        "yet', not a validated all-clear — don't read it as vindication of a high "
        "self-reported drift. See raw_governance.risk_attribution for the full "
        "provenance." + tail
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
    """Surface prior discoveries the canonical payload already carries.

    `relevant_prior_work` is the check-in path's contribution: the formatter
    already builds it from the mirror's KG lookup and puts it in the response,
    but nothing read it here, so `memory_suggestions` stayed empty on the one
    tool that had prior work to offer.

    `relevant_discoveries` arrives as {"message": ..., "discoveries": [...]}
    rather than a bare list, which the previous isinstance check discarded.
    """
    payload = _harvest_payload(payload)
    candidates = payload.get("relevant_discoveries")
    if isinstance(candidates, dict):
        candidates = candidates.get("discoveries")
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
                    "similarity",
                    # the mirror path scores its hits as `relevance`; without it
                    # a suggestion arrives with no indication of match strength
                    "relevance",
                )
                or item
            )
        else:
            suggestions.append({"summary": str(item)})
    return suggestions or None


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

    Canonical tools are unchanged. Read aliases default to their compact
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
        arguments.get("response_mode") or "compact"
    ).strip().lower()
    return response_mode == "full", (
        "Re-call search_shared_memory(response_mode='full') for the complete result set."
    )


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

    next_action: Any = None
    state_summary: Optional[Dict[str, Any]] = None

    if canonical_name == "onboard":
        next_action = (
            "Save agent_uuid and client_session_id, then check in with "
            "sync_state(response_text='...', complexity=0.5, "
            "client_session_id=...) as you work."
        )
        state_summary = _lift(payload, "lineage_state", "session_key")
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
        if coherence is not None:
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

    elif canonical_name == "knowledge":
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
        )
        note = source_payload.get("confidence_note")
        if note:
            state_summary["confidence_note"] = note

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
        state_summary = _lift(payload, "session_id", "phase", "reviewer")
        session_id = state_summary.get("session_id", "...")
        next_action = (
            "Review session open - submit your position with "
            f"dialectic(action='thesis', session_id='{session_id}', root_cause='...')."
        )

    if next_action is not None:
        envelope["next_action"] = _friendly_action_hint(next_action)
    if state_summary:
        envelope["state_summary"] = state_summary

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

    suggestions = _memory_suggestions(payload)
    if suggestions:
        envelope["memory_suggestions"] = suggestions
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

    include_raw, raw_hint = _raw_governance_policy(friendly_name, arguments)
    if include_raw:
        envelope["raw_governance"] = payload
    else:
        envelope["raw_governance_available"] = True
        if raw_hint:
            envelope["raw_governance_hint"] = raw_hint
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
