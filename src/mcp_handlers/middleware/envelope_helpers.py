"""Helper functions and constants for the agent-experience envelope middleware.

Extracted from ``envelope_step.py`` to keep projection, recovery-hint,
action-summary, memory-suggestion, and search-budget helpers modular.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from config.governance_config import GovernanceConfig
from src.checkpoints import CHECKPOINT_SCHEMA
from src.logging_utils import get_logger
from src.state_persistence import project_state_persistence
from src.tool_modes import progressive_aware_hint
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

_MEMORY_SUGGESTION_LIMIT = 3
_SEARCH_MORE_ROW_LIMIT = 17
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


_MEMORY_SUMMARY_PREVIEW_CHARS = 240


_MEMORY_BY_LABEL_CHARS = 64


_MEMORY_TAG_LIMIT = 5


_RELATED_DISCOVERY_LIMIT = 5


_RELATED_SUMMARY_PREVIEW_CHARS = 120


_SYNC_ROUTINE_BUDGET_BYTES = 2_500


_SEARCH_LEAN_BUDGET_BYTES = 3_000


_SEARCH_FULL_MODE_ROUTE = "search_shared_memory(response_mode='full')"


_SEARCH_FULL_MODE_HINT = f"Re-call {_SEARCH_FULL_MODE_ROUTE} for the complete result set."


_SEARCH_MORE_BUDGET_BYTES = 2_000


_SEARCH_MORE_SUMMARY_CHARS = 100


_START_SESSION_BUDGET_BYTES = 1_350


_START_SESSION_SIBLING_BUDGET_BYTES = 1_700


_START_SESSION_NOTICE_ALLOWANCE_BYTES = 300


_ONBOARD_RAW_MODES = frozenset({"full", "verbose", "standard"})


_ONBOARD_LIFT_KEYS = (
    "agent_id",
    "display_name",
    "is_new",
    "identity_resolution_outcome",
    "session_resolution_source",
)


_ROUTINE_MINT_OUTCOMES = frozenset({"minted_force_new", "minted_fresh"})


_RECORDED_LINEAGE_STATES = frozenset({"declared", "provisional", "confirmed"})


_ONBOARD_ASSURANCE_KEYS = ("tier", "session_source", "caller_proven", "baseline")


_FRESH_MINT_ASSURANCE_NOTE = (
    " Tier describes this mint only; "
    "check_working_state(client_session_id=...) confirms threading."
)


def _lift(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Copy the named keys out of payload when present and non-None."""
    return {k: payload[k] for k in keys if payload.get(k) is not None}


def _harvest_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Use nested canonical payloads when a caller hands us an envelope."""
    raw = payload.get("raw_governance")
    return raw if isinstance(raw, dict) else payload


def _checkpoint_uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _project_checkpoint_receipt(value: Any) -> Optional[Dict[str, Any]]:
    """Lift only the bounded, internally consistent checkpoint wire contract."""
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        return None
    retry = value.get("acceptance_retry_idempotent")
    if type(retry) is not bool:
        return None
    projected = {
        "schema": CHECKPOINT_SCHEMA,
        "status": value.get("status"),
        "acceptance_retry_idempotent": retry,
    }
    if value.get("status") == "unconfirmed":
        if not _checkpoint_uuid(value.get("attempt_id")):
            return None
        projected["attempt_id"] = value["attempt_id"]
        return projected
    if value.get("status") != "accepted":
        return None
    if not all(_checkpoint_uuid(value.get(key)) for key in ("checkpoint_id", "accepted_attempt_id")):
        return None
    if value.get("event_id") != value["checkpoint_id"]:
        return None
    previous = value.get("prev_checkpoint_id")
    if previous is not None and not _checkpoint_uuid(previous):
        return None
    for key in ("identity_id", "seq"):
        number = value.get(key)
        if type(number) is not int or not 0 < number <= 2**63 - 1:
            return None
    recorded_at = value.get("recorded_at")
    if not isinstance(recorded_at, str) or len(recorded_at) > 64:
        return None
    try:
        if datetime.fromisoformat(recorded_at).tzinfo is None:
            return None
    except ValueError:
        return None
    if type(value.get("idempotent_replay")) is not bool:
        return None
    projected.update({key: value[key] for key in (
        "checkpoint_id", "event_id", "identity_id", "seq", "recorded_at",
        "accepted_attempt_id", "idempotent_replay",
    )})
    projected["prev_checkpoint_id"] = previous
    assessment = value.get("assessment")
    if isinstance(assessment, dict):
        assessor = assessment.get("assessor")
        status = assessment.get("status")
        if (
            isinstance(assessor, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", assessor)
            and isinstance(status, str)
            and status in {"started", "completed", "failed", "unconfirmed", "not_repeated"}
            and assessment.get("automatic_replay") is False
        ):
            projected["assessment"] = {
                "assessor": assessor, "status": status, "automatic_replay": False,
            }
            meaning = assessment.get("completion_means")
            if meaning == "execution_returned; effects_are_not_atomic":
                projected["assessment"]["completion_means"] = meaning
    return projected


def _checkpoint_receipt_only(payload: Dict[str, Any], receipt) -> bool:
    """Capture acknowledgement does not supply a new operational decision."""
    if "checkpoint" not in payload:
        return False
    return (
        payload.get("success") is False
        or "error" in payload
        or payload.get("assessment_repeated") is False
        or (receipt is not None and (
            receipt["status"] == "unconfirmed" or receipt.get("idempotent_replay") is True
        ))
        or _decision_action(payload) is None
    )


def _checkpoint_only_envelope(friendly_name, source, receipt, arguments):
    """A small acknowledgement with no current-state inference, even in full mode."""
    envelope = {
        "success": source.get("success", True) is True,
        "tool": friendly_name,
        "next_action": (
            "Keep this checkpoint acknowledgement as evidence; "
            "no new assessment or policy decision is supplied."
        ),
    }
    if receipt is not None:
        envelope["checkpoint"] = receipt
    for key in ("agent_id", "agent_uuid"):
        value = source.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128 and not any(c.isspace() for c in value):
            envelope[key] = value
    for key in ("message", "error"):
        value = _one_line(source.get(key), limit=500)
        if value is not None:
            envelope[key] = value
    code = source.get("error_code")
    if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
        envelope["error_code"] = code
    if type(source.get("assessment_repeated")) is bool:
        envelope["assessment_repeated"] = source["assessment_repeated"]
    # Preserve an explicit assessment-write acknowledgement independently;
    # replay never carries a new assessment acknowledgement.
    if receipt is None or receipt.get("idempotent_replay") is not True:
        persistence = project_state_persistence(source.get("state_persistence"))
        if persistence is not None:
            envelope["state_persistence"] = persistence
    if canonical_response_mode((arguments or {}).get("response_mode") or "auto") == "full":
        envelope["raw_governance"] = {key: value for key, value in envelope.items() if key != "tool"}
    return envelope


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
    if envelope.get("recovery_hint") or envelope.get("warnings"):
        # A warning is the formatter's own reason to expand auto to mirror.
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


def _drop_all_clear_status(envelope: Dict[str, Any]) -> None:
    """Keep a bare "healthy" off the check-in's friendly state_summary.

    ``status`` / ``health_status`` are bands computed from the state
    estimate: "healthy" means risk below HealthThresholds.risk_healthy_max
    with no active void (and, for ``status``, coherence above its critical
    line). Lifted as the word "healthy", they sat beside "not a validated
    all-clear" on a provisional verdict and beside a coherence value labelled
    "not health-rated" (AX dogfood 2026-10-16, item 10), and the contract's
    deployed posture keeps health language off produced readings. The
    response already carries the risk itself (``risk_score``, and
    ``risk_summary`` in advisory words), so the word added nothing but the
    contradiction.

    Only the all-clear value goes. "moderate" and "critical" stay, because
    they ask the reader to look, and "critical" can come from the void or
    coherence checks that the risk fields do not cover. The canonical payload,
    raw_governance and every persisted field keep the value unchanged. No
    caller reads it from this surface (the routine shape already drops the
    pair, see _drop_routine_proceed_duplicates), so its absence is nothing a
    consumer can depend on. Runs after the routine check, which still keys on
    the positive "healthy" evidence.
    """
    state = envelope.get("state_summary")
    if not isinstance(state, dict):
        return
    for key in ("status", "health_status"):
        if state.get(key) == "healthy":
            state.pop(key)


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


_COHERENCE_BADGE_KEYS = ("value", "status", "source", "role")


def _metrics_state_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The metrics read's state, each value said once.

    The canonical minimal tier annotates every EISV dimension as
    {value, label, range, note} and risk as {value, threshold, status}; that
    shape stays for canonical readers (the discord-bridge HUD parses it). Here
    they are bare values: the per-dimension contract rides once on this
    tool's description (EISV_INLINE_SUMMARY), and risk_summary carries the
    risk band. On the minimal tier coherence keeps its inline badge in the
    same {value, status, source, role} shape sync_state uses (#1872). The
    canonical standard tier builds coherence as a bare float with no source
    or role, so it passes through bare there (pre-existing; full carries
    legacy_diagnostics instead).
    """
    verdict = payload.get("verdict")
    if verdict is not None:
        state = dict(verdict) if isinstance(verdict, dict) else {"verdict": verdict}
    else:
        state = {}
    for key in (
        "status",
        "primary_eisv_source",
        "E",
        "I",
        "S",
        "V",
        "coherence",
        "risk_score",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            value = (
                {k: value[k] for k in _COHERENCE_BADGE_KEYS if k in value}
                if key == "coherence"
                else value.get("value")
            )
        if value is not None:
            state.setdefault(key, value)
    return state


_METRICS_STOP_DECISIONS = frozenset({"pause", "reject"})


def _metrics_decision(payload: Dict[str, Any]) -> Optional[str]:
    """The decision a metrics read's verdict rode on: explain_verdict's
    decision_action on the minimal and standard tiers, last_decision_action
    beside the full tier's bare-string verdict."""
    verdict = payload.get("verdict")
    decision = (
        verdict.get("decision_action") if isinstance(verdict, dict) else None
    )
    if decision is None:
        decision = payload.get("last_decision_action")
    return str(decision).lower() if decision is not None else None


def _metrics_verdict_step(
    payload: Dict[str, Any],
    decision: Optional[str],
) -> Any:
    """The verdict's recommended step, which follows the decision: the wrapped
    verdict's own next_action, or the glossary entry for a bare-string one."""
    verdict = payload.get("verdict")
    if isinstance(verdict, dict):
        return verdict.get("next_action") or None
    if isinstance(verdict, str) and verdict:
        try:
            from src.governance_glossary import explain_verdict

            return explain_verdict(verdict, decision_action=decision).get(
                "next_action"
            )
        except Exception:  # pragma: no cover - defensive; glossary is pure
            return None
    return None


def _metrics_next_action(payload: Dict[str, Any]) -> Any:
    """The next step a metrics read recommends, from the state it reports.

    unitares.lifecycle-envelope.v1 requires next_action on check_working_state.
    The canonical payload carries one only when uninitialized or unbound
    (next_action). A stop decision comes next: interpret_state's guidance is
    written without knowing the decision (governance_state._generate_guidance),
    so ahead of it a paused agent was told "Near basin boundary - state may
    flip. Maintain consistency."; the verdict's step follows the decision.
    Otherwise the standard tier's interpreted guidance, then the verdict's own
    step, then the full tier's interpreted guidance, then the glossary entry
    for the full tier's bare-string verdict with the decision it rode on.
    """
    if payload.get("next_action"):
        return payload["next_action"]
    decision = _metrics_decision(payload)
    if decision in _METRICS_STOP_DECISIONS:
        step = _metrics_verdict_step(payload, decision)
        if step:
            return step
    if payload.get("guidance"):
        return payload["guidance"]
    verdict = payload.get("verdict")
    if isinstance(verdict, dict) and verdict.get("next_action"):
        return verdict["next_action"]
    state = payload.get("state")
    if isinstance(state, dict) and state.get("guidance"):
        return state["guidance"]
    return _metrics_verdict_step(payload, decision)


def _metrics_identity_assurance(
    payload: Dict[str, Any],
    binding_assurance: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Mark a real metrics read on a call whose session the server only
    inferred.

    No self-read reaches this with an inferred binding any more. The /mcp/
    identity step and the REST prebind never bind a pre_onboard read to a
    server-inferred source (no sticky consult for a read, and no agent_uuid
    or X-Agent-Id read proof), so such a read returns the unbound payload,
    which has no agent_id and gets nothing here: for self-reads the mark is
    purely defensive.

    It is still reached by an explicit agent_id (through use_tool, or
    check_working_state over REST; the /mcp/ schema drops the argument) on a
    call whose session was only inferred (a fingerprint, an onboard pin, a
    transport-injected REST id). That reading is of the agent the caller
    named, and caller_proven=false says nothing on the call proves the caller
    is that agent. check_working_state's description says so.

    The canonical payload cannot carry the mark: its agent_signature is
    {"uuid": null} for an unbound caller. So the request's assurance comes
    from the dispatch context (``binding_assurance``), with the signature as
    a fallback. A caller-proven read gets nothing; an unbound payload (no
    agent_id) is not a reading of anyone.
    """
    if payload.get("agent_id") is None:
        return None
    assurance = binding_assurance
    if not isinstance(assurance, dict):
        signature = payload.get("agent_signature")
        signature = signature if isinstance(signature, dict) else {}
        assurance = signature.get("identity_assurance")
    if not isinstance(assurance, dict):
        return None
    if (
        assurance.get("caller_proven") is not False
        or assurance.get("proof_origin") != "server_inferred"
    ):
        return None
    return _lift(assurance, "tier", "caller_proven", "session_source")


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
        "Policy margin is near an edge - keep scope tight, sync_state only at "
        "a meaningful milestone when you also hold a judgment the substrate "
        "cannot infer, and use self_recovery(action='review', "
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
        "text, sync_state only at a meaningful milestone when you also hold "
        "a judgment the substrate cannot infer, and use "
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
            + " - keep scope tight and sync_state only at a meaningful milestone "
            "when you also hold a judgment the substrate cannot infer. "
            f"{_review_refusal_phrase(review_limit)}, so it is not a step here."
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
            + " - keep scope tight, sync_state only at a meaningful milestone "
            "when you also hold a judgment the substrate cannot infer, "
            "and use self_recovery(action='review', reflection='...') "
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


def _resolves(payload: Dict[str, Any], path: tuple[str, ...]) -> bool:
    """True when ``path`` names a present, non-None value inside ``payload``."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or node.get(key) is None:
            return False
        node = node[key]
    return True


def _verdict_caveat(
    source_payload: Dict[str, Any],
    *,
    include_raw: bool,
    state_summary: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
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

    The provenance pointer names only a path present in the response being
    returned: the envelope's own ``state_summary.evidence`` when it carries
    one, otherwise a ``raw_governance`` path, and only when raw is included.
    A bounded ``sync_state`` has neither, and its evidence basis is already
    stated inline, so it gets no pointer: the pointer used to name
    ``raw_governance`` on responses that omit it (#2332 dropped raw from
    bounded check-ins after #1867 wrote the paths), and the only way to act on
    it there was another check-in, which writes. Presence is unchanged, so
    ``_is_routine_proceed`` still keys on the caveat alone.
    """
    verdict_confidence, evidence_basis = _verdict_assurance(source_payload)
    if verdict_confidence != "provisional":
        return None
    attribution = source_payload.get("risk_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    primary_driver = attribution.get("primary_driver")
    discriminability = attribution.get("discriminability")
    discriminability = discriminability if isinstance(discriminability, dict) else {}
    cold_start = _is_cold_start(source_payload)
    non_discriminative = discriminability.get("non_discriminative") is True
    until = discriminability.get("updates_until_baseline")
    tail = ""
    if isinstance(until, int) and until > 0:
        tail = f" ~{until} more check-in(s) until the behavioral signal is weighted."
    evidence_path = None
    if isinstance(state_summary, dict) and isinstance(state_summary.get("evidence"), dict):
        evidence_path = "state_summary.evidence"
    elif include_raw:
        candidates = [
            ("verdict", "evidence"),
            ("metrics", "verdict", "evidence"),
            ("metrics", "primary_eisv_source"),
            ("primary_eisv_source",),
        ]
        if cold_start or non_discriminative:
            candidates.insert(0, ("risk_attribution",))
        evidence_path = next(
            (
                "raw_governance." + ".".join(path)
                for path in candidates
                if _resolves(source_payload, path)
            ),
            None,
        )
    pointer = f" See {evidence_path} for provenance." if evidence_path else ""
    if cold_start:
        message = (
            "Verdict is provisional: behavioral confidence is below the assessment "
            "threshold. 'safe'/'proceed' means no trouble detected under the "
            "cold-start prior, not a validated all-clear."
        )
    else:
        message = (
            "Verdict is provisional: the behavioral baseline is still forming. "
            "'safe'/'proceed' is not a validated all-clear."
        )
    return (
        f"{message} Evidence basis: {evidence_basis or 'unspecified'}."
        + pointer
        + tail
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


def _explicit_search_limit(arguments: Optional[Dict[str, Any]]) -> Optional[int]:
    """The `limit` the caller passed, or None when they left it unset."""
    value = (arguments or {}).get("limit")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        # The typed link the record carries, so a linked store_finding
        # confirms it in the ack. Absent when the record has none.
        "response_to",
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
    # update_finding is not advertised in progressive mode; point at use_tool.
    return progressive_aware_hint(result)


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


_PREDICTION_BOUND_OUTCOME_HINT = (
    "There is no read by outcome id. This outcome is bound to its "
    "prediction_id, so repeating this identical call under the same identity "
    "(pass client_session_id) with "
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


_ROUTINE_REVIEW_KEYS = frozenset({
    "success",
    "server_time",
    "agent_signature",
    "session_id",
    "phase",
    "whose_move",
    "next_call",
    "next_step",
    "one_call_review",
    "thesis_source",
    "reviewer_agent_id",
    "awaiting_reviewer",
    "orchestrated_review",
    "reviewer_dispatch",
    "reviewed_work_authorship",
    "paused_agent_id",
    "session_type",
    "reason",
    "trigger_source",
    "message",
    "note",
})


_ROUTINE_REVIEW_PHASES = frozenset({"thesis", "antithesis"})


def _review_ack_blockers(payload: Dict[str, Any]) -> List[str]:
    """What keeps a request_review ack from being routine, in a stable order.

    Empty means routine: positive evidence of an open review waiting on a
    known move, and nothing else. A missing session_id or phase is a blocker,
    so a producer regression cannot be trimmed into the routine shape.
    """
    blockers = sorted(key for key in payload if key not in _ROUTINE_REVIEW_KEYS)
    if not payload.get("session_id"):
        blockers.append("no_session_id")
    phase = str(payload.get("phase") or "").strip().lower()
    if phase not in _ROUTINE_REVIEW_PHASES:
        blockers.append(f"phase:{phase or 'missing'}")
    if payload.get("success") is not True:
        blockers.append("success_not_true")
    return blockers


def _review_ack_raw_policy(
    payload: Dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """Decide whether a request_review ack repeats its canonical payload.

    A routine ack (see _ROUTINE_REVIEW_KEYS) omits it: the envelope already
    lifts the session id, phase, whose move it is and the next call, and the
    rest was the caller's request echoed back plus the agent signature, about
    2 KB of a 3 KB response measured live (#2443). Anything else keeps it.

    request_review declares no response_mode on /mcp/, so there is no full
    request to honour; the canonical dialectic(action='request') still returns
    the whole payload. The hint names the read that returns the session as it
    stands now. It is not this ack, so raw_governance_available is not set,
    and it never tells the caller to repeat request_review, which refuses with
    SESSION_EXISTS while this review is open.
    """
    include_raw = bool(_review_ack_blockers(payload))
    session_id = payload.get("session_id") or "..."
    hint = (
        f"dialectic(action='get', session_id='{session_id}') reads this "
        "review as it stands now (not this ack's payload); do not repeat "
        "request_review to see it."
    )
    return include_raw, hint


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


_FULL_ONBOARD_SHAPE_KEYS = frozenset({
    "session_resolution_source",
    "continuity_token_supported",
    "identity_context",
    "date_context",
    "ownership_proof_version",
    "welcome_message",
    "force_new_applied",
    "session_continuity",
    "next_calls_ref",
    "next_calls",
    "system_activity",
    "skill_resource",
    "tool_mode",
    "workflow",
})


_RESIDENT_REGISTRATION_STATUSES = frozenset({
    "registered",
    "not_on_roster",
    "no_roster_configured",
    "caller_supplied_tags",
})


def _compact_resident_registration(value: Any) -> Optional[Dict[str, Any]]:
    """A named mint's resident-registration verdict, without the prose.

    Onboard attaches one to every fresh mint that passes a name, and for an
    ordinary agent it reads not_on_roster or no_roster_configured: ~450-600 B
    explaining how resident registration works. The verdict stays visible, so
    a resident bootstrapped off the roster still sees that it was not
    registered; not_on_roster keeps what that costs and the producer's
    remedy (roster, restart, a fresh mint), since the tags cannot be added to
    this identity later. None when the block is not a shape this knows, which keeps
    the full record.
    """
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    on_roster = value.get("on_roster")
    if status not in _RESIDENT_REGISTRATION_STATUSES or not isinstance(on_roster, bool):
        return None
    compact: Dict[str, Any] = {"status": status, "on_roster": on_roster}
    if status == "not_on_roster":
        required = " + ".join(value.get("required_tags") or []) or "the resident tags"
        roster = value.get("roster_env") or "the resident roster"
        compact["detail"] = (
            f"Not on {roster}: no {required}; not protected from auto-archive. "
            "Granted only at mint, only to roster names; this identity cannot "
            "gain them. Fix: roster the name, restart, mint fresh."
        )
    elif status == "caller_supplied_tags":
        # Same reason not_on_roster keeps a detail: whatever tags this mint
        # carried are all it will ever carry.
        required = " + ".join(value.get("required_tags") or []) or "the resident tags"
        compact["detail"] = (
            f"Caller-supplied tags: {required} present only if passed at this "
            "mint; this identity cannot gain them later."
        )
    return compact


_BOOTSTRAP_WRITTEN_KEYS = frozenset({"written", "state_id", "next_step"})


def _compact_bootstrap(value: Any) -> Optional[Dict[str, Any]]:
    """The bootstrap check-in a caller asked for (initial_state), when written.

    Three short fields, lifted whole: the state_id and the note that the row
    is provisional exist only in this response.
    """
    if (
        isinstance(value, dict)
        and value.get("written") is True
        and set(value) <= _BOOTSTRAP_WRITTEN_KEYS
    ):
        return dict(value)
    return None


_ROUTINE_MINT_NOTICES = {
    "resident_registration": _compact_resident_registration,
    "bootstrap": _compact_bootstrap,
}


def _lift_mint_notices(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The compact form of each mint notice the payload carries."""
    lifted: Dict[str, Any] = {}
    for key, compact in _ROUTINE_MINT_NOTICES.items():
        value = payload.get(key)
        if value in (None, False, "", [], {}):
            continue
        compacted = compact(value)
        if compacted is not None:
            lifted[key] = compacted
    return lifted


def _thread_context_blocker(
    thread_context: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """What in the mint's thread position needs the record, or None.

    Positive evidence only: a missing or unknown episode_fork_kind is not
    routine. A root node ("none") is. So is a fresh uuid on a thread earlier,
    unrelated process-instances occupied ("sibling_locus", the common case
    behind a shared IP:UA fingerprint): the envelope lifts the earlier node's
    uuid, the fork kind and the sentence saying co-location does not establish
    lineage. A declared, provisional or rejected lineage, any spawn reason
    other than the default new_session, or a sibling whose earlier nodes were
    pruned (no uuid to lift beside the sentence) is something to read, so it
    keeps the record.
    """
    kind = thread_context.get("episode_fork_kind")
    if kind not in ("none", "sibling_locus"):
        return f"thread_context.episode_fork_kind={kind or 'missing'}"
    lineage_fork = thread_context.get("identity_lineage_fork")
    if lineage_fork is not False:
        shown = "missing" if lineage_fork is None else json.dumps(lineage_fork)
        return f"thread_context.identity_lineage_fork={shown}"
    # Any declared spawn reason other than the default is a claim to read,
    # including "explicit" without a parent: the caller meant succession and
    # got none, and the reason appears nowhere in the routine shape.
    spawn_reason = thread_context.get("spawn_reason")
    if spawn_reason not in (None, "new_session"):
        return f"thread_context.spawn_reason={spawn_reason}"
    if kind == "none":
        for key in ("predecessor", "is_fork"):
            if thread_context.get(key):
                return f"thread_context.{key}"
        return None
    # The routine sibling shape says co-location is not lineage only beside
    # the earlier node's uuid. With the earlier nodes pruned there is no uuid
    # to lift, so keep the record and its honest_message.
    predecessor = thread_context.get("predecessor")
    if not (isinstance(predecessor, dict) and predecessor.get("uuid")):
        return "thread_context.predecessor=missing"
    lineage_state = payload.get("lineage_state")
    if lineage_state != "no_lineage_declared":
        return f"lineage_state={lineage_state or 'missing'}"
    if payload.get("provisional_lineage"):
        return "provisional_lineage"
    return None


def _routine_mint_blockers(
    payload: Dict[str, Any], shape_keys: frozenset = frozenset()
) -> List[str]:
    """What keeps a mint from being routine, each named by its field.

    Empty for a fresh mint that went as asked: nothing about it needs
    explaining. Positive evidence only. A missing outcome is not routine, so a
    producer regression that drops the field shows the full record instead of
    looking like a clean mint. Any non-empty key outside _ROUTINE_MINT_KEYS
    that is not a notice in a shape _ROUTINE_MINT_NOTICES knows (a label
    rename, a deprecation, an unknown key) also keeps the record.
    ``shape_keys`` are keys the requested shape carries on any mint, which
    therefore say nothing about this one.
    """
    blockers: List[str] = []
    is_new = payload.get("is_new")
    if is_new is not True:
        blockers.append(f"is_new={'missing' if is_new is None else json.dumps(is_new)}")
    outcome = payload.get("identity_resolution_outcome")
    if outcome not in _ROUTINE_MINT_OUTCOMES:
        blockers.append(f"identity_resolution_outcome={outcome or 'missing'}")
    lineage_state = payload.get("lineage_state")
    if lineage_state not in (None, "no_lineage_declared"):
        # Declared, provisional or rejected lineage is something to read.
        blockers.append(f"lineage_state={lineage_state}")
    if payload.get("provisional_lineage"):
        blockers.append("provisional_lineage")
    for key, value in payload.items():
        if key in _ROUTINE_MINT_KEYS or key in shape_keys:
            continue
        if value in (None, False, "", [], {}):
            continue
        compact = _ROUTINE_MINT_NOTICES.get(key)
        if compact is None or compact(value) is None:
            blockers.append(key)
    thread_context = payload.get("thread_context")
    if isinstance(thread_context, dict):
        blocker = _thread_context_blocker(thread_context, payload)
        if blocker:
            blockers.append(blocker)
    else:
        # Positive evidence: onboard omits thread_context when it could not
        # place the mint (thread lookup raised, or no thread_id). Unknown
        # thread position is not a root node, so the record is shown whole.
        blockers.append("thread_context=missing")
    assurance = payload.get("identity_assurance")
    if not isinstance(assurance, dict):
        # Positive evidence: a mint whose assurance block is missing is shown
        # whole, not trimmed into a response with no assurance at all.
        blockers.append("identity_assurance=missing")
    elif _onboard_assurance_is_abnormal(assurance):
        blockers.append("identity_assurance")
    return list(dict.fromkeys(blockers))


def _is_routine_mint(payload: Dict[str, Any]) -> bool:
    """A fresh mint that went as asked (see _routine_mint_blockers)."""
    return not _routine_mint_blockers(payload)


def _start_session_budget(envelope: Dict[str, Any]) -> int:
    """The byte ceiling of a routine start_session, by mint class."""
    state = envelope.get("state_summary")
    sibling = isinstance(state, dict) and state.get("episode_fork_kind") == "sibling_locus"
    notices = sum(1 for key in _ROUTINE_MINT_NOTICES if key in envelope)
    return (
        (_START_SESSION_SIBLING_BUDGET_BYTES if sibling else _START_SESSION_BUDGET_BYTES)
        + notices * _START_SESSION_NOTICE_ALLOWANCE_BYTES
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
                "include_details=true for inline previews (imports capped at 2 KiB), or open one record "
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
            "include_details=true for inline previews (imports capped at 2 KiB), or open one record "
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
        current = resolve_metrics_verbosity(arguments)
        options = {
            "current": current,
            "routine": "verbosity='minimal' (default)",
            "interpreted_state": "verbosity='standard'",
            "complete_diagnostics": "verbosity='full'",
            "compatibility_aliases": "lite=true is minimal; lite=false is full",
        }
        verdict = payload.get("verdict")
        verdict_value = verdict.get("value") if isinstance(verdict, dict) else verdict
        if current == "full" and verdict_value == "unbound":
            # An unbound full read returns what 'standard' does, so pointing
            # complete_diagnostics back at verbosity='full' sends the caller
            # round the same call. Say why full had nothing more instead.
            # Recovery stays with next_action: it is cause-aware, and an
            # established process that only lost its client_session_id must
            # retry with its existing proof, not mint a second identity.
            del options["complete_diagnostics"]
            options["full_unavailable_reason"] = (
                "unbound: full adds nothing until this session is bound; "
                "follow next_action to bind it"
            )
        return options
    return None


def _mode_in_effect(
    friendly_name: str,
    arguments: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]],
) -> Optional[str]:
    """The response mode this call was served in, read from the request.

    Resolved from the arguments (and sync_state's resolved ``_mode``), not
    from ``response_options``: that block is absent in exactly the responses
    where a size hint could name the mode already in effect (a bounded
    sync_state, a truncated search, the default check_working_state).
    """
    arguments = arguments or {}
    if friendly_name == "sync_state":
        return (payload or {}).get("_mode") or canonical_response_mode(
            arguments.get("response_mode") or "auto"
        )
    if friendly_name == "check_working_state":
        return resolve_metrics_verbosity(arguments)
    if friendly_name == "search_shared_memory":
        return str(arguments.get("response_mode") or "lean").strip().lower()
    return None


def _attach_response_size(
    envelope: Dict[str, Any],
    friendly_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Expose response cost before callers discover it through context pressure.

    The byte count intentionally excludes this metadata field, avoiding a
    self-referential size calculation while staying within a few dozen bytes of
    the final serialized payload.

    ``reduce_with`` never names the mode already in effect: advice to switch
    to the mode the caller is in cannot be acted on. start_session gets none
    at all, since acting on it would mean minting another identity; its size
    receipt stays.
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
        current = _mode_in_effect(friendly_name, arguments, payload)
        reduce_with = None
        if friendly_name == "search_shared_memory":
            # Name only levers that shrink this response. Outside full mode
            # normalize_compact_search_details already forces
            # include_details=false, so it can matter only in full.
            open_one = (
                "open one discovery with knowledge(action='details', "
                "discovery_id='...')"
            )
            if current == "full":
                # Name include_details=false only when details are actually
                # inline in this response, whether the caller asked for them
                # or the handler auto-included them for 1-3 results
                # (_resolve_detail_inclusion).
                discoveries = (payload or {}).get("discoveries") or []
                details_inline = any(
                    isinstance(d, dict) and d.get("details") for d in discoveries
                )
                levers = (
                    "include_details=false, response_mode='lean' or a lower limit"
                    if details_inline
                    else "response_mode='lean' or a lower limit"
                )
                reduce_with = f"Use {levers}; {open_one}."
            elif current == "compact":
                reduce_with = f"Use response_mode='lean'; {open_one}."
            else:
                reduce_with = f"{open_one[0].upper()}{open_one[1:]}."
        elif friendly_name == "sync_state" and current == "full":
            reduce_with = (
                "Use response_mode='compact' for routine check-ins or 'mirror' "
                "for actionable diagnostics."
            )
        elif friendly_name == "sync_state":
            reduce_with = (
                "Use response_mode='minimal' only when the bare action/EISV "
                "snapshot is sufficient."
            )
        elif friendly_name == "check_working_state" and current == "full":
            reduce_with = (
                "Use verbosity='standard' for EISV, verdict, risk_score, basin "
                "and mode without the diagnostics, or verbosity='minimal'."
            )
        elif friendly_name == "check_working_state":
            reduce_with = "Use verbosity='minimal'."
        if reduce_with and not (current and f"'{current}'" in reduce_with):
            metadata["reduce_with"] = reduce_with
    envelope["_response_size"] = metadata


def _prune_resolution_notes_hint(envelope: Dict[str, Any], notes_in_more: bool) -> None:
    """Keep note coaching only while a digest or bounded extra row uses it."""
    suggestions = envelope.get("memory_suggestions") or []
    if any(isinstance(row, dict) and "resolution_notes" in row for row in suggestions):
        return
    if notes_in_more and len(suggestions) == _MEMORY_SUGGESTION_LIMIT:
        return
    envelope.pop("resolution_notes_hint", None)


_MIXED_RELEVANCE_NOTE = (
    "relevance values on different relevance_basis are not comparable; "
    "list order is the ranking."
)


def _note_mixed_relevance(envelope: Dict[str, Any]) -> None:
    """Say once that scores on different bases do not compare.

    A hybrid search can show a lexical-rank-only score (around 0.016) next to
    a semantic similarity (around 0.5) in one list; read as one scale, the
    first looks like a non-match. Written only while the shown digests carry
    two or more bases (2026-10-16 dogfood read, item 2).
    """
    state = envelope.get("state_summary")
    if not isinstance(state, dict):
        return
    suggestions = envelope.get("memory_suggestions")
    bases = {
        item.get("relevance_basis")
        for item in suggestions or []
        if isinstance(item, dict) and item.get("relevance_basis")
    } if isinstance(suggestions, list) else set()
    if len(bases) > 1:
        state["relevance_note"] = _MIXED_RELEVANCE_NOTE
    else:
        state.pop("relevance_note", None)


_STALENESS_NOTE = (
    "last_activity_days (stale results only) = days since last write; "
    "verify before acting."
)


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
    # Lean search has already omitted the tier ladder. This final wording
    # reduction preserves its executable route and frees enough space for
    # the attribution-omitted marker even at the exact budget boundary.
    if envelope.get("raw_governance_hint") == _SEARCH_FULL_MODE_HINT:
        steps.append(("raw_governance_hint", _SEARCH_FULL_MODE_ROUTE))
    return steps


def _truncate_search_projection(
    envelope: Dict[str, Any], wire_bytes, *, notes_in_more: bool = False,
) -> None:
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
            _prune_resolution_notes_hint(envelope, notes_in_more)
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
            for key in (
                "status",
                "superseded_by",
                "last_activity_days",
                "imported_memory",
                "closure_class",
                "closure_standard",
                "resolution_notes",
            ):
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
            _prune_resolution_notes_hint(envelope, notes_in_more)

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
    marker's ~36 bytes (lean's full-route wording alone can free 37;
    compact's tier ladder and `expand_with` can free more), so withheld attribution there is
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


