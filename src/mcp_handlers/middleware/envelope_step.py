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
      "raw_governance": {...}       # the full canonical payload
    }

Population is conservative: every field is harvested from values the
canonical handlers already return — this layer reorders and translates,
it does not compute new governance signals. Fields with nothing to say
are omitted. Error payloads (success=False / "error") pass through
unchanged: the raw error contract carries its own recovery info.

The step must never break a response: any parse/build failure returns
the original handler result untouched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Recovery thresholds quoted to agents — same pair the registry teaches
# for the quick_resume / self_recovery_review split (tool_stability's
# direct_resume_if_safe migration note).
_RECOVERY_COHERENCE_FLOOR = 0.60
_RECOVERY_RISK_CEILING = 0.40

_MEMORY_SUGGESTION_LIMIT = 3


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
        parts.append(f"coherence {coherence:.2f}")
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

    The quick_resume/self_recovery thresholds are recovery-tool thresholds,
    not a live-state alarm. Coherence often sits near 0.5 in healthy governed
    operation, so coherence alone must not produce a recovery warning.
    """
    risky = risk is not None and risk >= _RECOVERY_RISK_CEILING
    attention = _needs_attention(payload)
    if not (risky or attention):
        return None
    action = _decision_action(payload) or _verdict_value(payload)
    severe = action in {"pause", "reject", "block", "stop"} or (
        risk is not None and risk >= 0.7
    )
    drifting = coherence is not None and coherence < _RECOVERY_COHERENCE_FLOOR
    continuing = action in {"proceed", "continue", "approve", "ok", "healthy"}
    if severe or (attention and drifting and not continuing):
        return (
            "Working state looks degraded - pause and call "
            "self_recovery_review(reflection='...') before continuing."
        )
    if attention and drifting and continuing:
        return (
            "Coherence is near an edge - keep scope tight, sync_state after "
            "the next substantial step, and use self_recovery_review(reflection='...') "
            "only if work stalls."
        )
    return (
        "Risk is elevated - if you feel stuck, quick_resume() applies when "
        f"coherence > {_RECOVERY_COHERENCE_FLOOR:.2f} and risk < "
        f"{_RECOVERY_RISK_CEILING:.2f}; otherwise self_recovery_review()."
    )


def _memory_suggestions(payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Surface prior discoveries the canonical payload already carries."""
    payload = _harvest_payload(payload)
    candidates = (
        payload.get("relevant_discoveries")
        or payload.get("results")
        or payload.get("discoveries")
    )
    if not isinstance(candidates, list) or not candidates:
        return None
    suggestions = []
    for item in candidates[:_MEMORY_SUGGESTION_LIMIT]:
        if isinstance(item, dict):
            suggestions.append(
                _lift(item, "discovery_id", "id", "summary", "title", "similarity")
                or item
            )
        else:
            suggestions.append({"summary": str(item)})
    return suggestions or None


def build_experience_envelope(
    friendly_name: str,
    canonical_name: str,
    payload: Dict[str, Any],
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
        state_summary.update(_lift(decision, "action", "margin", "nearest_edge"))
        if coherence is not None:
            state_summary["coherence"] = coherence
        if risk is not None:
            state_summary["risk_score"] = risk
        next_action = (
            "Keep working - sync_state again after your next substantial step, "
            "and record_result(...) when an outcome lands."
        )

    elif canonical_name == "get_governance_metrics":
        # This handler already speaks the friendly dialect - map directly.
        next_action = payload.get("next_action") or payload.get("guidance")
        verdict = payload.get("verdict")
        if verdict is not None:
            state_summary = verdict if isinstance(verdict, dict) else {"verdict": verdict}

    elif canonical_name == "knowledge":
        candidates = source_payload.get("results") or source_payload.get("discoveries") or []
        total = source_payload.get("total_count")
        if total is None:
            total = source_payload.get("count")
        if total is None:
            total = len(candidates)
        next_action = (
            f"{total} prior discoveries matched - read before redoing work. "
            "Full context: knowledge(action='details', discovery_id=...). "
            "Record new findings: knowledge(action='store', summary='...')."
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
        state_summary = _lift(payload, "session_id", "phase", "reviewer")
        session_id = state_summary.get("session_id", "...")
        next_action = (
            "Review session open - submit your position with "
            f"dialectic(action='thesis', session_id='{session_id}', root_cause='...')."
        )

    if next_action is not None:
        envelope["next_action"] = next_action
    if state_summary:
        envelope["state_summary"] = state_summary

    risk_text = _risk_summary(coherence, risk)
    if risk_text:
        envelope["risk_summary"] = risk_text

    suggestions = _memory_suggestions(payload)
    if suggestions:
        envelope["memory_suggestions"] = suggestions

    hint = _recovery_hint(source_payload, coherence, risk)
    if hint:
        envelope["recovery_hint"] = hint

    envelope["raw_governance"] = payload
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

        envelope = build_experience_envelope(invoked, name, payload)
        return [TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))]
    except Exception:
        logger.warning(
            "experience envelope failed for %r - returning raw response",
            getattr(ctx, "original_name", name),
            exc_info=True,
        )
        return result
