"""Post-execution step: normalized agent-experience envelope.

Alias-gated: only calls invoked via an `experience=True` alias in
tool_stability (start_session, sync_state, check_working_state,
search_shared_memory, store_finding, update_finding, record_result,
request_review) get their response reshaped. Canonical tool names stay
byte-identical, so no existing client contract changes.

Envelope shape (friendly fields first, raw payload available on demand):

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
are omitted. Default read aliases, bounded ``sync_state`` modes and the write
aliases ``store_finding``, ``update_finding`` and ``record_result`` omit the
repeated canonical payload and advertise an explicit full-response escape
hatch; the write aliases first lift the ids and warnings a caller needs next.
A plain fresh ``start_session`` omits it too, without a hint, because the only
way to act on one would be another mint. Other state-changing aliases retain
it. The documented full mode restores it explicitly (``verbosity="full"`` on
``check_working_state``, ``response_mode="full"`` elsewhere), except on the
finding writes, whose route to the stored record is a
``knowledge(action="details")`` read. That read returns the record as stored, not the ack: write-time warnings
and the store-time similarity snapshot are lifted into the ack itself because
no later read returns them. A routine ``sync_state`` proceed also says each
fact once (``_drop_routine_proceed_duplicates``); guide, pause and provisional
responses keep their full shape.
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

from config.governance_config import GovernanceConfig
from src.logging_utils import get_logger
from src.mcp_handlers.response_formatter import (
    canonical_response_mode,
    normalize_discovery_list,
)
from src.mcp_handlers.support.param_normalization import (
    FRIENDLY_SEARCH_DETAIL_POLICY_KEY,
    FRIENDLY_SEARCH_DETAILS_REQUESTED_KEY,
    resolve_metrics_verbosity,
)

logger = get_logger(__name__)

# Recovery threshold quoted to agents — the same risk ceiling used by the
# quick_resume contract. Legacy coherence is directional controller feedback
# and is deliberately absent from recovery guidance.
_RECOVERY_RISK_CEILING = 0.40


def _review_risk_limit() -> float:
    """Reviewed self-recovery's risk gate: review passes only below it.

    handle_self_recovery_review (lifecycle/operations.py) admits
    ``risk_score < 0.65`` and so refuses AT the limit; the check action's
    eligibility test uses ``>`` and disagrees on the boundary. Hints follow
    the path that actually refuses, so callers compare with ``>=``.

    Imported lazily: the lifecycle handler module pulls in storage and the
    tool registry, which the middleware must not load at import time.
    """
    try:
        from src.mcp_handlers.lifecycle.self_recovery import MAX_RISK_FOR_SELF_RECOVERY

        return float(MAX_RISK_FOR_SELF_RECOVERY)
    except Exception:  # pragma: no cover - defensive; the constant is 0.65
        return 0.65


def _operator_resume_limit() -> float:
    """operator_resume_agent's hard risk limit (refuses above it, force or not)."""
    try:
        from src.mcp_handlers.lifecycle.self_recovery import OPERATOR_RESUME_HARD_RISK_LIMIT

        return float(OPERATOR_RESUME_HARD_RISK_LIMIT)
    except Exception:  # pragma: no cover - defensive; the constant is 0.80
        return 0.80


def _stopped_recovery_route(risk: Optional[float], review_limit: float, *, paused: bool) -> str:
    """Exits for a stop above the review gate, each one that will not refuse.

    Pausing auto-initiates a dialectic session by default, and request_review
    then answers SESSION_EXISTS, so the open session is named first. Operator
    resume refuses above its hard limit or with a void active.
    """
    text = (
        f"{_review_refusal_phrase(review_limit)}. If a dialectic review of this "
        "pause is open, dialectic(action='get', agent_id=<your agent UUID>) "
        "finds it and your thesis answers it; otherwise request_review opens one."
    )
    operator_limit = _operator_resume_limit()
    if risk is not None and risk > operator_limit:
        text += f" Operator resume refuses above risk {operator_limit:.2f}"
        text += ", so the other exit is the pause's expiry." if paused else "."
    else:
        text += " An operator can resume you unless a void is active."
    return text


def _review_refusal_phrase(limit: float) -> str:
    """State the review gate without overclaiming it.

    handle_self_recovery_review still admits an agent above the limit when
    the persisted row proves the legacy non-authored cold-start trap, so the
    refusal is not absolute.
    """
    return (
        f"Reviewed self-recovery refuses at risk {limit:.2f} and above "
        "(except where it finds the legacy cold-start trap)"
    )

_MEMORY_SUGGESTION_LIMIT = 3
_MEMORY_SUMMARY_PREVIEW_CHARS = 240
# Bound on a digest's `by` display label. `agent_id` is never bounded: it is
# the identity key a reader passes back as `agent_id_filter`, so a prefix of it
# would silently name nobody (or the wrong writer).
_MEMORY_BY_LABEL_CHARS = 64
_MEMORY_TAG_LIMIT = 5
# store_finding's related_discoveries is the store-time similarity snapshot:
# the findings this write resembled when it was stored. Its ids are also the
# stored record's related_to, so the write-time content a details read cannot
# give back is the summary previews. The ack keeps a bounded list of ids and
# short previews; each record is one details read away.
_RELATED_DISCOVERY_LIMIT = 5
_RELATED_SUMMARY_PREVIEW_CHARS = 120
_SYNC_ROUTINE_BUDGET_BYTES = 2_500
_SEARCH_LEAN_BUDGET_BYTES = 3_000
# Default start_session. continuity_token alone is ~330 B of it.
_START_SESSION_BUDGET_BYTES = 1_200
_ONBOARD_RAW_MODES = frozenset({"full", "verbose", "standard"})
# Onboard fields a caller or adapter reads off the friendly envelope. The
# plugin's post-identity hook and identity_sidecar read agent_id/display_name
# from the top level or from raw_governance, so lifting them is what lets the
# default response drop raw_governance.
# session_resolution_source, when a payload carries it, is what the plugin's
# post-identity hook caches and its identity-contract audit checks.
_ONBOARD_LIFT_KEYS = (
    "agent_id",
    "display_name",
    "is_new",
    "identity_resolution_outcome",
    "session_resolution_source",
)
# The resolution outcomes of a mint that went as asked. A resume miss, a
# reactivated archive or any resumed binding is not routine: the envelope then
# carries the whole onboard record, because those facts exist only in the
# mint's own response (identity() re-reads the binding, not how it was made).
_ROUTINE_MINT_OUTCOMES = frozenset({"minted_force_new", "minted_fresh"})
# Compact identity_assurance for a mint that went as asked. The operator guide
# tells agents to confirm the binding from tier and session_source here.
# The minimal onboard payload carries its source only here (it has no
# top-level session_resolution_source), so session_source stays.
# baseline stays: "fresh_identity" is what says a weak mint binding is
# expected, not a deficiency to fix.
_ONBOARD_ASSURANCE_KEYS = ("tier", "session_source", "caller_proven", "baseline")

_ACTION_ALIASES = {
    "approve": ("proceed", None),
    "continue": ("proceed", None),
    "healthy": ("proceed", None),
    "ok": ("proceed", None),
    "safe": ("proceed", None),
    "caution": ("proceed", "guide"),
    "guide": ("proceed", "guide"),
    "resumed": ("proceed", "resumed"),
    "not_paused": ("proceed", "not_paused"),
    "block": ("pause", "block"),
    "high-risk": ("pause", "high-risk"),
    "reject": ("pause", "reject"),
    "stop": ("pause", "stop"),
}

_COMPACT_READ_ALIASES = frozenset({
    "check_working_state",
    "search_shared_memory",
})

# Write aliases whose acknowledgement omits the repeated canonical payload by
# default. Each one lifts the identifiers a caller needs next (and the warnings
# a writer must see) before the omission, so the ack stays self-sufficient.
# request_review is deliberately absent: its ack carries the review itself
# (resolution conditions, reviewer dispatch, thesis-failure flags), which the
# envelope does not project, and it declares no response_mode on /mcp/.
# start_session is absent too: its ack shape is decided separately, with the
# rest of the identity/onboarding surface.
_COMPACT_WRITE_ALIASES = frozenset({
    "store_finding",
    "update_finding",
    "record_result",
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
    if risk < GovernanceConfig.RISK_APPROVE_THRESHOLD:
        band = "low"
    elif risk < GovernanceConfig.RISK_REVISE_THRESHOLD:
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
    """The action the policy decided, read before any verdict vocabulary.

    Mirror mode drops `decision` and surfaces the behavioral verdict instead
    (see `_agent_facing_verdict_raw`), and a metrics read carries no decision at
    all. A guided proceed then arrives as verdict "high-risk", which
    `_ACTION_ALIASES` maps to pause, so reading the verdict value first told an
    agent that was never paused that its action was "pause". Order, most
    authoritative first: the final `decision`; an applied runtime enforcement
    (the agent IS paused); the final decision a wrapped verdict rode on
    (`explain_verdict`'s `decision_action`); then `policy_evaluation`, which is
    built before post-ODE dialectic enforcement can escalate the decision
    (updates/phases.py) and so may be stale; then the verdict value.
    """
    decision = payload.get("decision")
    if decision is not None and not isinstance(decision, dict):
        return str(decision).lower()
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    verdicts = [
        v for v in (payload.get("verdict"), metrics.get("verdict")) if isinstance(v, dict)
    ]
    enforcement = payload.get("enforcement")
    enforced_pause = (
        "pause"
        if isinstance(enforcement, dict) and enforcement.get("applied") is True
        else None
    )
    policy = payload.get("policy_evaluation")
    decided = [
        (decision or {}).get("action"),
        enforced_pause,
        *(v.get("decision_action") for v in verdicts),
        payload.get("last_decision_action"),
        policy.get("action") if isinstance(policy, dict) else None,
    ]
    for value in decided:
        if value is not None:
            return str(value).lower()
    for container in (decision, payload.get("verdict"), payload):
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


def _is_cold_start(payload: Dict[str, Any]) -> bool:
    """True only while the verdict is owned by the cold-start prior.

    Narrower than a "provisional" verdict: that grade also covers a
    behavioral reading that is not yet baselined (check-ins 3-24), which is a
    measurement of the agent, not the prior.
    """
    attribution = payload.get("risk_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    metrics = payload.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    primary_source = metrics.get("primary_eisv_source") or payload.get(
        "primary_eisv_source"
    )
    driver = attribution.get("primary_driver")
    basis = _verdict_evidence(payload).get("basis")
    if driver in {"behavioral_assessment", "independent_verification_floor"} or (
        primary_source == "behavioral"
    ):
        return False
    return (
        driver == "phi_cold_start"
        or primary_source in {"ode_fallback", "phi_cold_start"}
        or basis in {"ode_fallback", "phi_cold_start"}
    )


def _cold_start_pause_deferred(payload: Dict[str, Any], risk: Optional[float]) -> bool:
    """Whether this cold-start reading would have paused an authored report.

    The pause comes from the high-risk verdict, not a risk number: task-type
    adjustment can move risk_score below 0.7 while the verdict stays
    high-risk. Read what the guard recorded, then the verdict (mirror mode
    drops the decision), and fall back to the risk band only when neither
    is present.
    """
    decision = payload.get("decision")
    if isinstance(decision, dict) and (
        decision.get("cold_start_epistemic_deferred")
        or str(decision.get("original_action") or "").lower() == "pause"
    ):
        return True
    verdict = _verdict_value(payload)
    if verdict is not None:
        return verdict == "high-risk"
    return risk is not None and risk >= 0.7


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
    if not sub_action and inferred_action == "proceed":
        # recent_decisions stores the bare action ("proceed"); a guided
        # verdict ("caution"/"guide") still carries the guide sub_action.
        verdict_sub = _ACTION_ALIASES.get(str(_verdict_value(payload) or "").lower())
        if verdict_sub and verdict_sub[0] == "proceed" and verdict_sub[1]:
            sub_action = verdict_sub[1]

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
    if verdict_confidence == "provisional" and inferred_action not in {None, "uninitialized"}:
        summary["headline"] = (
            f"Provisional: {inferred_action}; behavioral evidence is still forming."
        )
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


_ROUTINE_VERDICTS = frozenset({"safe", "proceed", "approve"})


def _is_routine_proceed(envelope: Dict[str, Any]) -> bool:
    """A clean proceed: nothing in the envelope asks the agent to act.

    Anything else — a guide, a pause, a provisional or caveated verdict, a
    near edge, a review nudge, a recovery hint — keeps the full shape, because
    those are the responses where the repeated fields carry meaning. An
    unassessed edge does not disqualify: the coherence edge is unassessed for
    every agent today, so the margin keeps its scope and unassessed edges
    beside it on a routine proceed instead.
    """
    state = envelope.get("state_summary")
    if not isinstance(state, dict) or state.get("action") != "proceed":
        return False
    # Positive evidence only: a missing health, margin or verdict is not
    # routine, so a producer regression that drops one cannot be trimmed
    # into the same shape as a clean proceed.
    health = [state.get(key) for key in ("status", "health_status") if key in state]
    if not health or any(value != "healthy" for value in health):
        return False
    if state.get("margin") != "comfortable":
        return False
    if state.get("sub_action") not in (None, "approve"):
        return False
    if state.get("verdict_provisional") or envelope.get("verdict_caveat"):
        return False
    if envelope.get("recovery_hint"):
        return False
    if envelope.get("review_suggested") or "request_review" in str(
        envelope.get("next_action") or ""
    ):
        # A review nudge asks the agent to act.
        return False
    if state.get("nearest_edge"):
        return False
    summary = envelope.get("action_summary")
    summary = summary if isinstance(summary, dict) else {}
    if summary.get("headline") or summary.get("verdict_confidence") == "provisional":
        return False
    if summary.get("sub_action") not in (None, "approve"):
        return False
    verdict = summary.get("verdict")
    return verdict is not None and str(verdict).lower() in _ROUTINE_VERDICTS


def _drop_routine_proceed_duplicates(envelope: Dict[str, Any]) -> None:
    """Drop what restates a clean proceed, and say that it was trimmed.

    action_summary keeps action, reason and risk_score: it is the documented
    first read (docs/manual/04-integrating-agents.md), and the integration
    samples read action_summary.action. state_summary keeps what the Python SDK
    reads (agents/sdk/.../_checkin_fields.py: action, sub_action, coherence,
    risk_score) and the margin with its scope and unassessed edges: today the
    coherence edge is unassessed for every agent, so "comfortable" is only
    honest beside what it did not measure. What goes are the restatements: the
    approve sub_action and safe verdict in action_summary, an unspecified
    verdict_confidence, and the "healthy" status pair. _is_routine_proceed
    admits only positive evidence of each, so the marker below distinguishes
    "trimmed because routine" from "missing because of a bug".
    """
    summary = envelope.get("action_summary")
    if isinstance(summary, dict):
        for key in ("sub_action", "verdict"):
            summary.pop(key, None)
        if summary.get("verdict_confidence") == "unspecified":
            summary.pop("verdict_confidence", None)

    state = envelope.get("state_summary")
    if isinstance(state, dict):
        state.pop("status", None)
        state.pop("health_status", None)
    envelope["response_shape"] = "routine"


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
    stopped = action in {"pause", "reject", "block", "stop"}
    # High risk alone reads as severe only when no decision is known. Once the
    # policy has decided to continue (the cold-start guard, gap suppression),
    # "pause and call self_recovery" contradicts that decision, and reviewed
    # recovery then refuses the agent anyway (at or above
    # MAX_RISK_FOR_SELF_RECOVERY) after recording its reflection in shared
    # memory.
    decided_to_continue = action in {
        "proceed", "continue", "approve", "ok", "healthy", "safe", "guide",
        "resumed", "not_paused",
    }
    # Reviewed recovery refuses at or above this risk; no agent may be routed
    # to a review that will refuse it.
    review_limit = _review_risk_limit()
    recovery_refused = risk is not None and risk >= review_limit
    severe = stopped or (
        not decided_to_continue and risk is not None and risk >= 0.7
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
        if recovery_refused:
            # Reviewed recovery would record the reflection and then refuse at
            # this risk, so do not send a stopped agent there.
            return (
                "Working state looks degraded - pause this line of work. "
                + _stopped_recovery_route(risk, review_limit, paused=stopped)
            )
        return (
            "Working state looks degraded - pause and call "
            "self_recovery(action='review', reflection='...') before continuing."
        )
    if decided_to_continue and _is_cold_start(payload):
        # A cold-start reading is the prior, not a measurement of the agent.
        # This decision did not block. Whether the agent's own next report can
        # pause on the same prior depends on the guard's configuration; say so
        # only when it is true.
        hint = (
            "Cold start: this risk is the prior, not a measurement of your "
            "behavior, and "
            + ("nothing blocks you now." if action in {"resumed", "not_paused"}
               else "this decision does not block.")
        )
        # Only true while the cold-start guard leaves an agent's own report
        # out (COLD_START_GUARD_INCLUDE_AUTHORED off, or the guard disabled).
        authored_can_pause = not (
            GovernanceConfig.NON_AUTHORED_COLD_START_GUARD_ENABLED
            and GovernanceConfig.COLD_START_GUARD_INCLUDE_AUTHORED
        )
        if authored_can_pause and _cold_start_pause_deferred(payload, risk):
            hint += (
                " Until your third check-in your own sync_state is scored on the "
                "same prior and can pause on this reading."
            )
        return hint
    if risky and decided_to_continue and recovery_refused:
        return (
            "Risk is elevated but "
            + ("nothing blocks you now" if action in {"resumed", "not_paused"}
               else "this decision does not block")
            + " - keep scope tight and sync_state after your next substantial "
            f"step. {_review_refusal_phrase(review_limit)}, so it is not a step here."
        )
    if attention and continuing:
        return margin_hint if margin_is_near_edge else verdict_hint
    if risky and decided_to_continue:
        # Not paused and below the review gate: same advice the attention
        # branch gives a continuing agent, so two branches never disagree.
        return (
            "Risk is elevated but "
            + ("nothing blocks you now" if action in {"resumed", "not_paused"}
               else "this decision does not block")
            + " - keep scope tight, sync_state after your next substantial "
            "step, and use self_recovery(action='review', reflection='...') "
            "only if work stalls."
        )
    if risky and recovery_refused:
        # Unrecognised action (e.g. a bare "high-risk" verdict on a read):
        # quick and reviewed recovery both refuse at this risk.
        return (
            "Risk is elevated - "
            + _review_refusal_phrase(review_limit)[0].lower()
            + _review_refusal_phrase(review_limit)[1:]
            + "; if you are blocked, open a dialectic review with request_review."
        )
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


_UNKNOWN_WRITER = "unknown"
_DIGEST_ATTRIBUTION_KEYS = ("by", "by_truncated", "agent_id")


def _summary_preview(summary: str, limit: int) -> tuple[str, bool]:
    """Collapse whitespace and bound a summary at a word boundary.

    Returns the preview and whether it was cut, so a bounded preview can say
    so rather than posing as the complete summary.
    """
    compact = " ".join(summary.split())
    if len(compact) <= limit:
        return compact, False
    cutoff = compact.rfind(" ", 0, limit - 1)
    if cutoff < limit // 2:
        cutoff = limit - 1
    return compact[:cutoff].rstrip() + "…", True


def _compact_related_discoveries(
    payload: Dict[str, Any],
) -> tuple[Optional[List[Dict[str, Any]]], Optional[int]]:
    """Bound store_finding's store-time similarity snapshot for the ack.

    The canonical ``related_discoveries`` rows are whole records (minus
    details). The ack keeps only what a writer needs to decide whether to open
    or supersede one: its id and a short summary preview. Returns the compact
    rows and, when rows were dropped, the snapshot's full length.

    The store handler already stops its similarity scan at five rows, so the
    total is not reachable today; the cap here keeps the ack bounded if that
    handler limit is ever raised.
    """
    related = payload.get("related_discoveries")
    if not isinstance(related, list) or not related:
        return None, None
    rows: List[Dict[str, Any]] = []
    for item in related[:_RELATED_DISCOVERY_LIMIT]:
        if not isinstance(item, dict):
            continue
        row: Dict[str, Any] = {}
        discovery_id = item.get("id") or item.get("discovery_id")
        if discovery_id is not None:
            row["discovery_id"] = discovery_id
        summary = item.get("summary")
        if isinstance(summary, str):
            preview, truncated = _summary_preview(
                summary, _RELATED_SUMMARY_PREVIEW_CHARS
            )
            row["summary"] = preview
            if truncated:
                row["preview_truncated"] = True
        if row:
            rows.append(row)
    if not rows:
        return None, None
    total = len(related) if len(related) > _RELATED_DISCOVERY_LIMIT else None
    return rows, total


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
    from_prior_work = False
    if not candidates:
        from_prior_work = bool(payload.get("relevant_prior_work"))
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
            # Freshness travels as the handler's structured
            # `last_activity_days` (days since the last write, present only
            # past the staleness threshold), not its `staleness_warning`
            # sentence: three copies of the same suffix used to outbid
            # attribution for the digest budget. One response-level note
            # explains the field (_note_stale_digests).
            suggestion = _lift(
                item,
                "title",
                "type",
                "status",
                "last_activity_days",
            )
            # A superseded row names what replaced it, so "prefer the newer
            # entry" can be followed without a full-mode re-call. First id
            # only; details on this row lists every successor. A core field,
            # not attribution: the budget steps measure it before any
            # attribution is restored.
            successor = item.get("superseded_by")
            if isinstance(successor, list):
                successor = successor[0] if successor else None
            if successor:
                suggestion["superseded_by"] = str(successor)
            discovery_id = item.get("discovery_id") or item.get("id")
            if discovery_id is not None:
                suggestion["discovery_id"] = discovery_id

            # Attribution survives the digest: the canonical result leads with
            # `by` (the write-time label) and carries `_agent_id` (the
            # identity), and a lean reader asking who wrote a finding must not
            # need a second, full-mode call to learn it. The identity is
            # copied whole; only the display label is bounded, and a bounded
            # label says so rather than posing as the complete value.
            # `relevant_prior_work` rows are the one exception: the mirror
            # formatter writes the writer's id into `by` (response_formatter
            # `_format_mirror`), so there `by` is the identity, not a label.
            # "unknown" is the producers' placeholder for a missing id and is
            # neither a label nor an identity.
            by = item.get("by")
            agent_id = item.get("_agent_id") or item.get("agent_id")
            if from_prior_work and not agent_id:
                agent_id, by = by, None
            if isinstance(by, str) and by and by != _UNKNOWN_WRITER:
                if len(by) > _MEMORY_BY_LABEL_CHARS:
                    suggestion["by"] = by[: _MEMORY_BY_LABEL_CHARS - 1] + "…"
                    suggestion["by_truncated"] = True
                else:
                    suggestion["by"] = by
            if agent_id and str(agent_id) != _UNKNOWN_WRITER:
                suggestion["agent_id"] = str(agent_id)

            summary = item.get("summary")
            if isinstance(summary, str):
                preview, truncated = _summary_preview(
                    summary, _MEMORY_SUMMARY_PREVIEW_CHARS
                )
                suggestion["summary"] = preview
                if truncated:
                    suggestion["preview_truncated"] = True

            tags = item.get("tags")
            if isinstance(tags, list):
                suggestion["tags"] = tags[:_MEMORY_TAG_LIMIT]
                if len(tags) > _MEMORY_TAG_LIMIT:
                    suggestion["tags_truncated"] = True

            # Lean projections expose one score plus its basis. Score maps are
            # useful in the canonical response but make a three-result digest
            # surprisingly expensive and force callers to understand ranking
            # internals merely to choose which record to open.
            score_fields = (
                ("relevance", "relevance"),
                ("fusion_score", "fusion"),
                ("rrf_score", "rrf"),
                ("similarity", "semantic"),
                ("score", "score"),
            )
            for field, default_basis in score_fields:
                value = item.get(field)
                if value is not None:
                    suggestion["relevance"] = value
                    suggestion["relevance_basis"] = (
                        item.get("relevance_basis") or default_basis
                    )
                    break
            if "relevance" not in suggestion and item.get("relevance_basis"):
                suggestion["relevance_basis"] = item["relevance_basis"]

            if any(
                key in item for key in ("has_details", "details", "details_preview")
            ):
                suggestion["has_details"] = bool(
                    item.get("has_details")
                    or item.get("details")
                    or item.get("details_preview")
                )
            suggestions.append(suggestion or {"summary": ""})
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
        "related_to",
    )
    if not summary.get("related_to"):
        # The record's links: on store_finding, the similar findings this
        # write auto-linked; on update_finding, the stored record's existing
        # links (an update does not auto-link). Worth a pointer when there
        # are any, noise when there are none. A store_finding ack that lifts
        # related_discoveries drops this copy (same ids; see the caller).
        summary.pop("related_to", None)
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


# Keys whose values are the caller's own words (a finding's summary, a review's
# reasoning), echoed back. Translating tool names inside them rewrote user text:
# a summary saying "auto-onboard" came back as "auto-start_session".
_CALLER_TEXT_KEYS = frozenset({
    "summary",
    "details",
    "content",
    "reasoning",
    "root_cause",
    "response_text",
    "issue_description",
    "title",
})


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
        key: item if key in _CALLER_TEXT_KEYS else _friendly_action_hint(item)
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


# record_result hints. There is no read by outcome id, so the only route back
# to an omitted outcome payload is the idempotent replay of a prediction-bound
# outcome; see _write_ack_raw_policy.
_PREDICTION_BOUND_OUTCOME_HINT = (
    "There is no read by outcome id. This outcome is bound to its "
    "prediction_id, so repeating this identical call with "
    "response_mode='full' returns its complete payload (including the full "
    "EISV snapshot semantics) as a replay (idempotent_replay: true) and "
    "records no second outcome, while the binding is retained. A call that "
    "changes the outcome is refused (PREDICTION_REUSE_CONFLICT). A repeat "
    "without a prediction_id records a second outcome."
)
_UNBOUND_OUTCOME_HINT = (
    "This outcome's full payload cannot be read again: there is no read by "
    "outcome id, and it has no prediction_id, so a repeat records a second "
    "outcome. To get the complete payload (including the full EISV snapshot "
    "semantics) inline, pass response_mode='full' on a later outcome."
)


def _is_alias_injected_action(friendly_name: str, arguments: Dict[str, Any]) -> bool:
    """True unless the call resolved to an action other than the alias's own.

    Arguments without an ``action`` (a direct call of the builder) count as
    the alias's own action; after resolve_alias the key is always present.
    The comparison folds case the way action_router does before routing.
    """
    from ..tool_stability import resolve_tool_alias

    _, alias = resolve_tool_alias(friendly_name)
    injected = getattr(alias, "inject_action", None)
    action = arguments.get("action")
    if not injected or action is None:
        return True
    return str(action).strip().lower() == str(injected).strip().lower()


def _write_ack_raw_policy(
    friendly_name: str,
    arguments: Dict[str, Any],
    payload: Dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """Decide whether a write acknowledgement repeats its canonical payload.

    Default: omit it. The envelope already lifts the ids a caller needs next,
    so the canonical copy was most of a typical ack and mostly repeated the
    agent signature and the stored record.

    Three things keep the payload inline. An explicit full request does, on
    record_result (``response_mode='full'``; outcome_event's
    ``include_semantics`` is the same request under an older name and survives
    ``/mcp/`` validation). A payload whose record id the envelope cannot lift
    does too, because then the canonical copy is the only place the caller
    could find out what was written. And a finding alias whose caller named an
    action other than the alias's own does, because the result is that
    action's answer, not a write ack (#2457).

    store_finding and update_finding have no full request here. This step sees
    the arguments after canonical validation, and KnowledgeParams fills
    ``response_mode='full'`` by default (the search alias works around the
    same default in normalize_compact_search_details), so a caller's explicit
    'full' and an omitted parameter arrive identical. A later
    knowledge(action='details') read returns the stored record, not
    everything this ack carried: the write-time warnings and a bounded
    related_discoveries snapshot (for its summary previews) are lifted into
    the ack for that reason. The
    canonical knowledge tool still returns the whole payload directly.
    outcome_event validates response_mode to None, so on record_result an
    arriving 'full' was the caller's.

    The hint never tells a caller to repeat a write that would write again: a
    second store mints a second finding. For the finding writes it names a
    details read, which returns the stored record. record_result has no read
    by outcome id. An outcome bound to a prediction_id is the one write a
    repeat does not duplicate: while its binding is retained, the identical
    call replays the stored outcome (``idempotent_replay``) and writes nothing,
    and the idempotency digest does not cover ``response_mode`` or
    ``include_semantics``, so repeating it with ``response_mode='full'``
    returns the full payload. Its hint names that route (a changed outcome is
    refused as PREDICTION_REUSE_CONFLICT, so the repeat must be identical).
    An outcome without a prediction_id has no such route: a repeat records a
    second outcome, so its hint names the full-mode parameter for a later
    outcome and says so.

    None of these acks sets raw_governance_available. Elsewhere that flag
    means a re-call that only reads; here the one route that returns the
    omitted payload is a repeat of the write, safe only for an identical
    prediction-bound call inside the binding's retention, and the hint states
    those conditions where a bare flag could not.
    """
    if friendly_name == "record_result":
        # include_semantics is read the way the handler reads it (the schema
        # lets a string through validation), so the ack keeps the payload
        # exactly when the handler built the full snapshot for it.
        from ..observability.outcome_events import _coerce_bool_flag

        full_mode = (
            str(arguments.get("response_mode") or "").strip().lower() == "full"
        )
        wants_full = full_mode or _coerce_bool_flag(arguments.get("include_semantics"))
        identifiable = payload.get("outcome_id") is not None
        # The handler sets idempotent_replay exactly when the outcome was
        # recorded against a prediction_id (outcome_events), so its presence,
        # not its value, says which route exists.
        hint = (
            _PREDICTION_BOUND_OUTCOME_HINT
            if "idempotent_replay" in payload
            else _UNBOUND_OUTCOME_HINT
        )
        return wants_full or not identifiable, hint

    # store_finding / update_finding: no full request (see above).
    if not _is_alias_injected_action(friendly_name, arguments):
        # The alias injects its action only when the caller sent none, and an
        # explicit one wins (resolve_alias). REST and stdio do not narrow
        # `action` out of these aliases, so update_finding(action='details',
        # discovery_id=...) runs a details read. That result is not a write
        # ack: its payload is what the caller asked for, and the details
        # hint would send it to repeat the read it just made (#2457).
        return True, None
    discovery = payload.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    discovery_id = (
        payload.get("discovery_id")
        or discovery.get("id")
        or arguments.get("discovery_id")
    )
    target = discovery_id if discovery_id is not None else "..."
    hint = (
        f"knowledge(action='details', discovery_id='{target}') returns the "
        "stored record, not this ack's payload; do not repeat the write to "
        "see it."
    )
    return discovery_id is None, hint


def _onboard_assurance_is_abnormal(assurance: Any) -> bool:
    """Whether a mint's identity_assurance block says something to act on.

    A just-minted identity is server-inferred by construction: the minting
    call cannot carry the client_session_id it is about to receive, so the
    block reports weak/not caller-proven and then explains that this is the
    expected baseline. Repeating that on every mint is a lecture, not a
    signal. Any other weak binding keeps the whole block, reason and
    how_to_strengthen included.
    """
    if not isinstance(assurance, dict):
        return False
    if assurance.get("caller_proven") is True and assurance.get("tier") == "strong":
        return False
    return assurance.get("baseline") != "fresh_identity"


# Every key a plain fresh minimal onboard carries (live shape, 2026-09-25).
# An allowlist, not a denylist: onboard adds keys after building the record
# (label_renamed, resident_registration, bootstrap, deprecations, ...), and a
# key this list does not know about must show the full record rather than be
# dropped as routine.
_ROUTINE_MINT_KEYS = frozenset({
    "success",
    "server_time",
    "welcome",
    "uuid",
    "agent_uuid",
    "agent_id",
    "display_name",
    "is_new",
    "client_session_id",
    "session_key",
    "identity_assurance",
    "next_step",
    "response_mode",
    "identity_resolution_outcome",
    "onboard_origin",
    "onboard_origin_basis",
    "lineage_state",
    "continuity_token",
    "thread_context",
    "provisional_lineage",
    "_response_size",
})


def _is_routine_mint(payload: Dict[str, Any]) -> bool:
    """A fresh mint that went as asked: nothing about it needs explaining.

    Positive evidence only. A missing outcome is not routine, so a producer
    regression that drops the field shows the full record instead of looking
    like a clean mint. Any non-empty key outside _ROUTINE_MINT_KEYS (a label
    rename, a resident-registration notice, a bootstrap write, a deprecation)
    also keeps the record.
    """
    if payload.get("is_new") is not True:
        return False
    if payload.get("identity_resolution_outcome") not in _ROUTINE_MINT_OUTCOMES:
        return False
    if payload.get("lineage_state") not in (None, "no_lineage_declared"):
        # Declared, provisional or rejected lineage is something to read.
        return False
    if payload.get("provisional_lineage"):
        return False
    for key, value in payload.items():
        if key not in _ROUTINE_MINT_KEYS and value not in (None, False, "", [], {}):
            return False
    thread_context = payload.get("thread_context")
    if isinstance(thread_context, dict) and (
        thread_context.get("predecessor") or thread_context.get("is_fork")
    ):
        return False
    assurance = payload.get("identity_assurance")
    if not isinstance(assurance, dict):
        # Positive evidence: a mint whose assurance block is missing is shown
        # whole, not trimmed into a response with no assurance at all.
        return False
    return not _onboard_assurance_is_abnormal(assurance)


def _raw_governance_policy(
    friendly_name: str,
    arguments: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Choose whether a friendly alias should repeat its canonical payload.

    Canonical tools are unchanged. Read aliases, bounded ``sync_state`` modes
    and the write aliases in ``_COMPACT_WRITE_ALIASES`` default to their
    bounded experience envelope and retain an explicit full-response escape
    hatch. Other state-changing aliases still repeat their payload.
    """
    if friendly_name == "sync_state":
        arguments = arguments or {}
        payload = payload or {}
        requested_mode = canonical_response_mode(
            arguments.get("response_mode") or "auto"
        )
        resolved_mode = payload.get("_mode")

        # Explicit full/verbose always wins. When auto has no resolved marker,
        # retain the historical raw payload: formatter full mode deliberately
        # has no `_mode`, and direct/helper callers may hand this layer a
        # canonical response. Actual compact/mirror/standard/minimal payloads
        # carry `_mode`, so routine calls can safely omit the duplicate.
        include_raw = requested_mode == "full" or (
            requested_mode == "auto" and resolved_mode is None
        )
        # Re-calling sync_state writes another check-in, so the hint names
        # the next call rather than a re-call.
        return include_raw, (
            "Pass response_mode='full' on the next sync_state for diagnostics."
        )

    if friendly_name in _COMPACT_WRITE_ALIASES:
        return _write_ack_raw_policy(friendly_name, arguments or {}, payload or {})

    if friendly_name == "start_session":
        # A mint that went as asked is fully described by the lifted fields,
        # so repeating the whole onboard record beneath them was the same
        # record twice. Anything else about the mint (resume miss, reactivated
        # archive, lineage, trajectory, abnormal assurance) keeps the record:
        # those facts exist only here. An explicit request uses the signals
        # onboard uses to pick its own verbose shape
        # (_derive_onboard_response_mode).
        arguments = arguments or {}
        payload = payload or {}
        requested = str(arguments.get("response_mode") or "").strip().lower()
        include_raw = (
            requested in _ONBOARD_RAW_MODES
            or (not requested and _as_bool(arguments.get("verbose"), default=False))
            or not _is_routine_mint(payload)
        )
        # No hint: the only way to act on one is another mint, and a mint that
        # was not routine already carries the record.
        return include_raw, None

    if friendly_name not in _COMPACT_READ_ALIASES:
        return True, None

    arguments = arguments or {}
    if friendly_name == "check_working_state":
        wants_full = (
            resolve_metrics_verbosity(arguments) != "minimal"
            or _as_bool(arguments.get("include_state"), default=False)
        )
        return wants_full, (
            "Re-call check_working_state(verbosity='standard') for EISV, verdict "
            "and basin with their meanings, or verbosity='full' for the complete "
            "canonical diagnostics."
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
        return {
            "current": resolve_metrics_verbosity(arguments),
            "routine": "verbosity='minimal' (default)",
            "interpreted_state": "verbosity='standard'",
            "complete_diagnostics": "verbosity='full'",
            "compatibility_aliases": "lite=true is minimal; lite=false is full",
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
        elif friendly_name == "check_working_state" and current == "full":
            metadata["reduce_with"] = (
                "Use verbosity='standard' for EISV, verdict, risk_score, basin "
                "and mode without the diagnostics, or verbosity='minimal'."
            )
        elif friendly_name == "check_working_state":
            metadata["reduce_with"] = "Use verbosity='minimal'."
    envelope["_response_size"] = metadata


def _enforce_search_projection_budget(envelope: Dict[str, Any]) -> None:
    """Keep the friendly KG digest inside its declared wire budget.

    Result summaries and tags are already bounded independently. This final
    guard protects the *whole* response from additive metadata growth. It only
    drops optional mode/retrieval coaching and then lower-ranked digests; the
    result count and explicit expansion route remain visible.
    """

    def wire_bytes() -> int:
        return len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    # Attribution never costs a result or its fields. Set it aside, run the
    # budget steps exactly as for an attribution-free payload (which decides
    # which digests and fields survive), then give each survivor back as much
    # attribution as still fits, in rank order: label and identity, else the
    # identity alone, else neither. An identity is only ever whole. One
    # envelope-level marker says something was withheld.
    suggestions = envelope.get("memory_suggestions")
    set_aside: List[Dict[str, Any]] = []
    if isinstance(suggestions, list):
        for item in suggestions:
            snap = {}
            if isinstance(item, dict):
                for key in _DIGEST_ATTRIBUTION_KEYS:
                    if key in item:
                        snap[key] = item.pop(key)
            set_aside.append(snap)

    # The staleness note is written before the budget steps so they measure
    # it, and re-checked after them: it goes if no stale digest survived.
    _note_stale_digests(envelope)
    if wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES:
        _truncate_search_projection(envelope, wire_bytes)
        _note_stale_digests(envelope)
    _restore_digest_attribution(envelope, set_aside, wire_bytes)


_STALENESS_NOTE = (
    "last_activity_days (stale results only) = days since last write; "
    "verify before acting."
)


def _note_stale_digests(envelope: Dict[str, Any]) -> None:
    """Explain `last_activity_days` once per response, only while a shown
    digest has it.

    The canonical per-row `staleness_warning` sentence is not lifted into the
    digest, so this is the one place the reader learns what the field means
    and that it calls for verification.
    """
    state = envelope.get("state_summary")
    if not isinstance(state, dict):
        return
    suggestions = envelope.get("memory_suggestions")
    if isinstance(suggestions, list) and any(
        isinstance(item, dict) and "last_activity_days" in item
        for item in suggestions
    ):
        state["staleness_note"] = _STALENESS_NOTE
    else:
        state.pop("staleness_note", None)


# The retrieval options a digest keeps when coaching yields to attribution:
# its tier, the open-one route, and the include_details override disclosure.
# A lean envelope carries no `normalized_parameters`, so `requested_tier` and
# `details_omitted_by` are the only in-band notice that include_details=true
# was not honoured; that is a disclosure, not coaching, so it never yields to
# attribution. Over-budget truncation (_truncate_search_projection) still cuts
# retrieval options to current_tier and open_one, as it did before; this
# tuple does not protect the disclosure from that step.
_RETRIEVAL_KEPT_FOR_ATTRIBUTION = (
    "current_tier",
    "open_one",
    "requested_tier",
    "details_omitted_by",
)


def _coaching_yields(envelope: Dict[str, Any]) -> List[tuple]:
    """The optional coaching that can give way to attribution, least useful
    first, as ``(key, what stays of it)``; ``None`` drops the key. None of it
    is a result or a field of one."""
    steps: List[tuple] = []
    # The tier ladder: raw_governance_hint still names full mode.
    if "response_options" in envelope:
        steps.append(("response_options", None))
    retrieval = envelope.get("discovery_retrieval_options")
    if isinstance(retrieval, dict):
        kept = {
            key: retrieval[key]
            for key in _RETRIEVAL_KEPT_FOR_ATTRIBUTION
            if retrieval.get(key) is not None
        }
        if kept and len(kept) < len(retrieval):
            steps.append(("discovery_retrieval_options", kept))
    # A truncated digest's pointer repeats the route raw_governance_hint names.
    if envelope.get("raw_governance_hint") and "expand_with" in envelope:
        steps.append(("expand_with", None))
    return steps


def _truncate_search_projection(envelope: Dict[str, Any], wire_bytes) -> None:
    """The budget steps proper, run on the attribution-free envelope: drop
    optional coaching, then lower-ranked digests, then compact the last."""
    envelope["projection_truncated"] = True
    envelope["expand_with"] = "search_shared_memory(..., response_mode='full')"
    envelope.pop("response_options", None)

    retrieval = envelope.get("discovery_retrieval_options")
    if isinstance(retrieval, dict):
        keep = {
            key: retrieval[key]
            for key in ("current_tier", "open_one")
            if retrieval.get(key) is not None
        }
        if keep:
            envelope["discovery_retrieval_options"] = keep

    suggestions = envelope.get("memory_suggestions")
    # The digest-set summary fields go in before the digests are measured, so
    # the budget below holds them too (written after the loop, they used to
    # push a digest that had just been fitted back over it). Only the count
    # changes afterwards, and a count of at most three keeps its width.
    state = envelope.get("state_summary")
    if isinstance(state, dict) and isinstance(suggestions, list):
        state["results_shown_in_digest"] = len(suggestions)
        state["result_set_truncated"] = True
    while (
        isinstance(suggestions, list)
        and suggestions
        and wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES
    ):
        if len(suggestions) > 1:
            suggestions.pop()
            continue

        # A single result can still carry unbounded historical fields (for
        # example, a legacy title or tag). Reduce the last surviving digest to
        # its stable handle, its lifecycle markers and a short preview before
        # dropping it entirely: a superseded row keeps its successor and a
        # stale one its age, so the survivor is not presented as current.
        item = suggestions[0]
        if isinstance(item, dict):
            compact = {}
            discovery_id = item.get("discovery_id")
            if discovery_id is not None:
                compact["discovery_id"] = str(discovery_id)[:128]
            for key in ("status", "superseded_by", "last_activity_days"):
                value = item.get(key)
                if value is not None:
                    compact[key] = value[:128] if isinstance(value, str) else value
            summary = item.get("summary")
            if isinstance(summary, str) and summary:
                compact["summary"] = summary[:96].rstrip() + (
                    "…" if len(summary) > 96 else ""
                )
            suggestions[0] = compact
        else:
            suggestions[0] = {"summary": str(item)[:96]}

        if wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES:
            suggestions.pop()

    # The digest set is final now, so its count is settled before any
    # attribution is restored: it must be inside the budget the restore
    # measures against, not appended after it.
    if isinstance(state, dict) and isinstance(suggestions, list):
        state["results_shown_in_digest"] = len(suggestions)


def _restore_digest_attribution(
    envelope: Dict[str, Any], set_aside: List[Dict[str, Any]], wire_bytes
) -> None:
    """Give each surviving digest back as much attribution as fits, in rank
    order: label and identity, else the identity alone, else neither.

    First without the withheld-marker: if everything fits, no marker is
    needed and its room is not taken from attribution. If something must be
    withheld, optional coaching gives way one piece at a time, least useful
    first (_coaching_yields), and the restore is retried after each, stopping
    once attribution is whole. Only if something must still be withheld is
    the restore redone with the marker's room reserved, so the marker says
    so. In a bounded search the room freed by then is always more than the
    marker's ~36 bytes (the tier ladder alone is ~190, and once truncation
    has dropped it `expand_with` is ~65), so withheld attribution there is
    always marked; the marker-free restore is the fallback for an envelope
    with nothing optional left. Last, each piece that gave way comes back,
    most useful first, if the whole piece still fits beside the attribution.
    Pieces move whole, so a small attribution need can still cost a whole
    piece (the ~190-byte ladder for a ~20-byte label); what changed is that
    coaching yields piece by piece, not all at once. The room left by then is
    smaller than any withheld attribution needed (each was tried with at
    least that much free), so nothing that comes back displaces one."""
    suggestions = envelope.get("memory_suggestions")
    if not isinstance(suggestions, list) or not any(set_aside[: len(suggestions)]):
        return

    def strip() -> None:
        for item in suggestions:
            if isinstance(item, dict):
                for key in _DIGEST_ATTRIBUTION_KEYS:
                    item.pop(key, None)

    def restore() -> bool:
        withheld = False
        for item, snap in zip(suggestions, set_aside):
            if not snap or not isinstance(item, dict):
                continue
            identity = {"agent_id": snap["agent_id"]} if "agent_id" in snap else {}
            for attempt in (snap, identity):
                if not attempt:
                    continue
                item.update(attempt)
                if wire_bytes() <= _SEARCH_LEAN_BUDGET_BYTES:
                    break
                for key in attempt:
                    item.pop(key, None)
            if any(key not in item for key in snap):
                withheld = True
        return withheld

    if not restore():
        return
    order = list(envelope)
    yielded: Dict[str, Any] = {}
    withheld = True
    for key, kept in _coaching_yields(envelope):
        yielded[key] = envelope[key]
        if kept is None:
            del envelope[key]
        else:
            envelope[key] = kept
        strip()
        withheld = restore()
        if not withheld:
            break
    if withheld:
        strip()
        envelope["digest_attribution_omitted"] = True
        if wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES:
            del envelope["digest_attribution_omitted"]
        restore()
    for key in reversed(list(yielded)):
        trimmed = envelope.get(key)
        envelope[key] = yielded[key]
        if wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES:
            if trimmed is None:
                del envelope[key]
            else:
                envelope[key] = trimmed
    if yielded:
        # A key that came back goes back to its place, not the end.
        rank = {key: index for index, key in enumerate(order)}
        items = sorted(envelope.items(), key=lambda kv: rank.get(kv[0], len(rank)))
        envelope.clear()
        envelope.update(items)


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
    # Resolve the tactical prediction id ONCE, canonical-source first.
    # _harvest_payload treats a nested raw_governance as the canonical payload,
    # so preferring the outer copy could hand back a stale id when a caller
    # passes an already-enveloped response carrying both (codex review, #2123).
    # Both the prose below and the top-level key use this one value.
    prediction_id = source_payload.get("prediction_id") or payload.get("prediction_id")
    if not (isinstance(prediction_id, str) and prediction_id):
        prediction_id = None
    coherence, risk = _coherence_and_risk(source_payload)
    include_raw, raw_hint = _raw_governance_policy(
        friendly_name,
        arguments,
        source_payload,
    )
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

    bounded_sync = friendly_name == "sync_state" and not include_raw
    sync_mode = source_payload.get("_mode") if bounded_sync else None
    routine_sync = bounded_sync and sync_mode == "compact"
    options = (
        None
        if bounded_sync
        else _response_options(friendly_name, source_payload, arguments)
    )

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
        # Lifted in every mode, so a client reading the top-level fields does
        # not lose them when it asks for response_mode='full'.
        envelope.update(_lift(payload, *_ONBOARD_LIFT_KEYS))
        if payload.get("display_name"):
            envelope["label_is"] = "social_or_cosmetic"
        assurance = payload.get("identity_assurance")
        if isinstance(assurance, dict):
            envelope["identity_assurance"] = (
                assurance
                if _onboard_assurance_is_abnormal(assurance)
                else _lift(assurance, *_ONBOARD_ASSURANCE_KEYS)
            )
        token = payload.get("continuity_token")
        if isinstance(token, str) and token:
            # Grouped with its caveat: the token is an advanced same-process
            # rebind proof, not something to attach to ordinary calls.
            envelope["rebind"] = {
                "continuity_token": token,
                "use_only_for": "identity(agent_uuid=..., continuity_token=..., resume=true)",
            }
        envelope["response_shape"] = "full" if include_raw else "routine"
        if isinstance(predecessor, dict) and predecessor.get("uuid"):
            state_summary["predecessor_uuid"] = predecessor["uuid"]
            fork_kind = payload.get("thread_context", {}).get("episode_fork_kind")
            lineage_fork = payload.get("thread_context", {}).get(
                "identity_lineage_fork"
            )
            if fork_kind == "identity_lineage" and lineage_fork is True:
                next_action += (
                    " Declared lineage for this fork is already recorded; do not "
                    "redeclare it on the current session."
                )
            else:
                next_action += (
                    " A prior node in this thread was detected, but thread "
                    "co-location does not establish lineage. Do not use its uuid "
                    "as parent_agent_id unless a future process is a deliberate "
                    "continuation after this process exits."
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
        # A paused check-in defaults to mirror mode, whose payload carries the
        # policy action only under the `verdict` wrapper, not `decision.action`,
        # so the lifts above missed it and a paused agent reading
        # state_summary.action saw nothing. action_summary already
        # resolved the action from every shape; reuse it here.
        action_summary = envelope.get("action_summary")
        action_summary = action_summary if isinstance(action_summary, dict) else {}
        for key in ("action", "sub_action"):
            if action_summary.get(key) is not None:
                state_summary.setdefault(key, action_summary[key])
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
        if state_summary.get("action") == "pause":
            # The generic continuation text below was emitted on pause verdicts
            # too, telling a paused agent to "keep working". Match recovery_hint.
            review_limit = _review_risk_limit()
            if risk is not None and risk >= review_limit:
                # Reviewed recovery records the reflection, then refuses here.
                next_action = (
                    "Paused - stop this line of work and do not continue it. "
                    + _stopped_recovery_route(risk, review_limit, paused=True)
                )
            else:
                next_action = (
                    "Paused - stop this line of work and do not continue it. Call "
                    "self_recovery(action='review', reflection='...') to request "
                    "resumption."
                )
        elif prediction_id:
            # Naming prediction_id here is what makes registry-bound
            # record_result discoverable — otherwise the outcome grades a
            # confidence borrowed from an unrelated earlier turn (fallback
            # binding dominates calibration rows, #2123). The id itself is
            # lifted beside this text, so it is named rather than repeated.
            next_action = (
                "Keep working; sync_state after your next substantial step. When "
                "an outcome lands, pass this prediction_id to record_result so it "
                "grades this check-in."
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
        # Write-time warnings and outcomes the handler reports beside the
        # record. With raw_governance omitted by default these would otherwise
        # vanish: a failed supersession, truncated content, an anonymous
        # writer, or a closure that declares no standard.
        envelope.update(_lift(
            source_payload,
            "agent_mode",
            "_identity_hint",
            "superseded",
            "_supersedes_warning",
            "superseded_by",
            "supersession_warning",
            "_name_hint",
            "_truncated",
            "_tip",
            "consolidation_hint",
            "closure_class_note",
        ))
        if "closure_class" in source_payload:
            envelope["closure_class"] = source_payload["closure_class"]
        # The store-time similarity snapshot. Its ids are the stored record's
        # related_to, which a details read returns; its summary previews are
        # what the details read cannot give back, so a bounded form stays in
        # the ack (consolidation_hint, lifted above, summarizes the same set).
        related, related_total = _compact_related_discoveries(source_payload)
        if related:
            envelope["related_discoveries"] = related
            if related_total is not None:
                envelope["related_discoveries_total"] = related_total
            if friendly_name == "store_finding":
                # store sets discovery.related_to to exactly these rows' ids,
                # so the summary copy would carry the same list twice.
                state_summary.pop("related_to", None)

        call_arguments = arguments or {}
        if not _is_alias_injected_action(friendly_name, call_arguments):
            # An explicit other action (details, get, supersede, update...)
            # ran that action, not this alias's own, so the alias's
            # "updated"/"stored" line would misreport it. The other action may
            # itself write (store, update, supersede, note...), so the line
            # names what ran and makes no claim about whether it wrote. Its
            # payload stays inline (#2457).
            ran = str(call_arguments.get("action")).strip().lower()
            next_action = (
                f"{friendly_name} ran knowledge(action='{ran}') because an "
                "explicit action was passed, not this alias's own action. "
                "That action's full response is under raw_governance; read "
                "it there before repeating the call, since a writing action "
                "writes again."
            )
        elif friendly_name == "store_finding":
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
        # `confidence_note` is not copied here: the envelope lifts it to the
        # top level for every alias (next to search_degraded_message), and a
        # second ~270 B copy was paid out of the lean digest's budget before
        # attribution. The `low_confidence` flag stays in both places.
        if retrieval_options and retrieval_options.get("current_tier"):
            state_summary["result_tier"] = retrieval_options["current_tier"]
        state_summary["results_shown_in_digest"] = min(
            len(candidates),
            _MEMORY_SUGGESTION_LIMIT,
        )
        if len(candidates) > _MEMORY_SUGGESTION_LIMIT:
            state_summary["result_set_truncated"] = True

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
            "corroboration_reasons",
            "corroboration_hint",
        )
        snapshot = payload.get("eisv_snapshot")
        if isinstance(snapshot, dict):
            compact = _compact_eisv(snapshot)
            if compact:
                state_summary["working_state"] = compact
        # Binding disclosure. The producer returns prediction_binding /
        # prediction_source / calibration_excluded precisely so a caller can
        # tell a registry-bound outcome from one that fell back to scraped
        # confidence — and a scraped-confidence row is excluded from
        # calibration entirely. Confined to raw_governance, those three left
        # the friendly surface reporting "Outcome recorded" identically either
        # way, so an agent could not tell a counted outcome from a dropped one.
        binding = _lift(
            source_payload,
            "prediction_binding",
            "prediction_source",
            "calibration_excluded",
        )
        state_summary.update(binding)
        # A replayed prediction_id and claims the grader could not verify
        # change what the recorded outcome means; the default ack no longer
        # repeats the canonical payload, so they ride here.
        for key, value in _lift(
            source_payload, "is_bad", "idempotent_replay", "unverified_fields"
        ).items():
            if value != []:
                state_summary[key] = value
        next_action = "Outcome recorded - continue, or sync_state to fold it into your working state."
        if binding.get("calibration_excluded"):
            next_action = (
                "Outcome recorded, but stamped calibration_excluded (binding: "
                f"{binding.get('prediction_binding') or 'unknown'}), so it does "
                "not train calibration. Pass the prediction_id from the "
                "sync_state you are grading to bind the outcome to it."
            )

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

    # Surface the tactical prediction id as a machine-readable field, not only
    # inside the next_action prose #1696 added. Every other formatter already
    # passes it through top-level (response_formatter _format_standard /
    # _format_mirror / _format_compact; minimal opts out on bandwidth grounds),
    # and update_response_service mints it onto the base payload with a
    # docstring naming top-level placement as the contract — this envelope,
    # which rebuilds the response from scratch, was the sole surface dropping
    # it. What an unbound record_result costs: prediction_source falls back to
    # "prev_confidence_fallback", which is in SCRAPED_PREDICTION_SOURCES, so
    # the row is stamped calibration_excluded and never reaches the tactical
    # lane (outcome_events.py); and decision_action for test_passed/test_failed
    # is hardcoded "proceed" rather than read off the registered prediction.
    # The prose stays — it tells the agent what the id is for (#2123).
    if prediction_id and canonical_name == "process_agent_update":
        envelope["prediction_id"] = prediction_id
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

    # Keep compatibility and verbosity metadata after the operational answer.
    if (
        canonical_name in {"process_agent_update", "get_governance_metrics"}
        and legacy
        and not routine_sync
    ):
        envelope["legacy_diagnostics"] = legacy
    if options:
        envelope["response_options"] = options

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

    if friendly_name in _COMPACT_WRITE_ALIASES and not include_raw:
        # A handler-side identity warning (e.g. record_result's
        # ephemeral_writer) must not vanish with the omitted payload. The
        # identity-warning step runs after this one and appends to this list.
        warnings = source_payload.get("identity_warnings")
        if isinstance(warnings, list) and warnings:
            envelope["identity_warnings"] = list(warnings)
        # Who the write was recorded under. The finding and outcome payloads
        # carry no top-level uuid: their attribution is success_response's
        # agent_signature, which rode only under raw_governance. A caller that
        # bound weakly (no client_session_id, a transport-fingerprint pin) has
        # to be able to see which identity it wrote as, and how well that
        # binding was proven, without asking for the full payload.
        if "agent_uuid" not in envelope:
            signature = source_payload.get("agent_signature")
            if isinstance(signature, dict) and signature.get("uuid"):
                envelope["agent_uuid"] = signature["uuid"]
                written_as = _lift(signature, "agent_id", "display_name")
                assurance = signature.get("identity_assurance")
                if isinstance(assurance, dict):
                    written_as.update(_lift(
                        assurance, "tier", "caller_proven", "proof_origin"
                    ))
                if written_as:
                    envelope["written_as"] = written_as

    if include_raw:
        envelope["raw_governance"] = payload
    else:
        # raw_governance_available promises a re-call that fetches the
        # omitted payload by reading. A routine start_session's record cannot
        # be fetched afterwards (only another mint would produce one), so it
        # does not claim one. The write acks have no such read either: a
        # finding's details read returns the stored record, not this ack's
        # payload, and record_result has no read by outcome id. Its one route
        # back is a repeat of the write, which replays instead of writing only
        # for an identical prediction-bound outcome while the binding is
        # retained; a bare flag cannot carry those conditions, so the hint
        # states them (see _write_ack_raw_policy).
        if (
            friendly_name != "start_session"
            and friendly_name not in _COMPACT_WRITE_ALIASES
        ):
            envelope["raw_governance_available"] = True
        if raw_hint:
            envelope["raw_governance_hint"] = raw_hint
    if routine_sync and _is_routine_proceed(envelope):
        _drop_routine_proceed_duplicates(envelope)
    bounded_search = friendly_name == "search_shared_memory" and not include_raw
    bounded_start = friendly_name == "start_session" and not include_raw
    if bounded_search:
        _enforce_search_projection_budget(envelope)

    # Routine sync responses should spend their budget on the decision, not on
    # a size receipt about the decision. Keep the receipt when the projection
    # itself misses its contract so the regression is visible in-band; other
    # tools retain their existing always-on measurement behavior.
    if bounded_sync:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        budget = _SYNC_ROUTINE_BUDGET_BYTES if routine_sync else 4_000
        if measured_bytes > budget:
            _attach_response_size(envelope, friendly_name)
    elif bounded_search:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        if measured_bytes > _SEARCH_LEAN_BUDGET_BYTES:
            _attach_response_size(envelope, friendly_name)
    elif bounded_start:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        if measured_bytes > _START_SESSION_BUDGET_BYTES:
            _attach_response_size(envelope, friendly_name)
    else:
        _attach_response_size(envelope, friendly_name)
    return envelope


def _refusal_with_friendly_tool(payload: dict, invoked: str, result):
    """Pass a typed refusal through, but name the tool the caller actually
    invoked.

    The refusal's ``tool`` is the canonical name (``process_agent_update``)
    because that is what the emission point knows. An agent that called
    ``sync_state`` should not be answered about a tool it never called — the
    same register-mismatch the envelope already guards against for
    ``next_action`` and ``state_summary`` (dogfood 2026-08-20). The Python SDK
    surfaces this field verbatim in ``IdentityRefusedError``, so it reaches a
    human. Nothing else is touched, and the payload is only rebuilt when the
    name actually differs.
    """
    if not invoked or payload.get("tool") == invoked:
        return result
    friendly = dict(payload)
    friendly["tool"] = invoked
    return [
        TextContent(type="text", text=json.dumps(friendly, ensure_ascii=False)),
        *result[1:],
    ]


async def apply_experience_envelope(name: str, arguments: Dict[str, Any], ctx, result):
    """POST_EXECUTION step. `name` is the canonical (post-alias) tool;
    the invoked name lives in ctx.original_name. Returns the (possibly
    reshaped) handler result; on ANY failure returns it untouched."""
    try:
        from ..identity_bootstrap import identity_refusal_status
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

        # A #425 typed identity refusal is success-SHAPED (`success: true`, no
        # `error` key), so the guard above does not catch it and the refusal
        # was being rebuilt as an ordinary check-in.
        #
        # Precisely: the recovery block was RELOCATED, not destroyed —
        # `hint` / `next_step` / `safe_options` / `do_not` / `status` all
        # survived under `raw_governance`, and `status` was also lifted into
        # `state_summary`. Two things were actually wrong. `next_action`
        # became the generic "Keep working - sync_state again after your next
        # substantial step", because `decision.get("action")` is None for a
        # refusal — an agent whose write was refused was told to continue. And
        # both shipped SDKs read the refusal at the TOP level, which the
        # envelope no longer had: `agents/sdk/.../errors.py` keys on
        # `rollout_flag`, and `elixir/unitares_sdk/.../envelope.ex` keys on
        # `status == "identity_required"` with two production outages pinned
        # as tests. Enveloping a refusal broke the detection both rely on.
        #
        # So the refusal passes through as its own contract. Everything the
        # envelope would add is derived from governance state that is never
        # computed before identity is proven, so there is nothing to lose.
        if identity_refusal_status(payload) is not None:
            return _refusal_with_friendly_tool(payload, invoked, result)

        envelope = build_experience_envelope(invoked, name, payload, arguments)
        return [TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))]
    except Exception:
        logger.warning(
            "experience envelope failed for %r - returning raw response",
            getattr(ctx, "original_name", name),
            exc_info=True,
        )
        return result
