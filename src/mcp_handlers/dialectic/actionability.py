"""Actionability, pause-evidence, synthetic-review, and readback helpers for dialectic handlers.

Extracted from ``dialectic/handlers.py`` to keep pure session projection,
actionability derivation, pause-evidence capture, and synthetic-reviewer
eligibility helpers modular.
"""

from typing import Dict, Any, Sequence, Optional, List
from mcp.types import TextContent
from dataclasses import dataclass
import math
import os
from numbers import Real
import re
import sys
from datetime import datetime, timezone

from .wait_assessment import assess_wait, suggests_facilitation
from src.dialectic_protocol import (
    NON_PROTOCOL_PHASES,
    DialecticSession,
)
from src.dialectic_reviewed_work import (
    read_reviewed_work_authorship,
)
from ..utils import error_response
from .beam_resolve_client import BeamDialecticPolicyRefusal
from .responses import (
    default_cooldown_steps,
    default_escalate_steps,
    default_resume_steps,
)
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as _default_mcp_server

logger = get_logger(__name__)


class _McpServerProxy:
    """Resolve ``mcp_server`` through ``dialectic.handlers`` so test monkeypatches take effect."""

    def __getattr__(self, name: str) -> Any:
        mod = sys.modules.get("src.mcp_handlers.dialectic.handlers")
        target = getattr(mod, "mcp_server", _default_mcp_server) if mod is not None else _default_mcp_server
        return getattr(target, name)


mcp_server = _McpServerProxy()

def _synthetic_review_budget(default: float = 55.0) -> float:
    """Wall-clock cap for the inline synthetic review (antithesis + synthesis).
    The handler timeouts below are DERIVED from this budget, so an overrun
    degrades to awaiting_facilitation (thesis already persisted) rather than
    killing the call. Measured typical: ~27s on gemma4. Tunable via
    UNITARES_DIALECTIC_REVIEW_BUDGET (read at import for the derived timeouts,
    so a change requires the usual server restart)."""
    raw = os.environ.get("UNITARES_DIALECTIC_REVIEW_BUDGET")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


REVIEW_DISPATCH_MARGIN = 35.0


SUBMIT_THESIS_TIMEOUT = _synthetic_review_budget() + REVIEW_DISPATCH_MARGIN  # 90.0 at the 55s default


ONE_CALL_CREATION_MARGIN = 15.0


REQUEST_REVIEW_TIMEOUT = SUBMIT_THESIS_TIMEOUT + ONE_CALL_CREATION_MARGIN  # 105.0 at the default


def _beam_policy_error_code(exc: BeamDialecticPolicyRefusal) -> str:
    return {
        "self_review_not_authorizing": "SELF_REVIEW_NOT_AUTHORIZING",
        "reviewer_recused": "REVIEWER_AUTHOR_CONFLICT",
        "invalid_review_authorship": "REVIEW_AUTHORSHIP_METADATA_INVALID",
        "session_identity_mismatch": "DIALECTIC_SESSION_IDENTITY_MISMATCH",
        "identity_proof_invalid": "DIALECTIC_IDENTITY_PROOF_INVALID",
        "identity_proof_replayed": "DIALECTIC_IDENTITY_PROOF_REPLAYED",
        "identity_verification_unavailable": "DIALECTIC_IDENTITY_VERIFICATION_UNAVAILABLE",
    }.get(exc.error_code, "DIALECTIC_POLICY_REFUSED")


def _beam_policy_recovery(exc: BeamDialecticPolicyRefusal) -> dict:
    if exc.error_code in {"identity_proof_invalid", "identity_proof_replayed"}:
        return {
            "action": "Retry with a caller-asserted session identity and fresh proof.",
            "related_tools": ["identity", "dialectic", "request_review"],
        }
    if exc.error_code == "identity_verification_unavailable":
        return {
            "action": "Retry after the lease-plane identity verifier recovers.",
            "related_tools": ["dialectic", "request_review"],
        }
    return {
        "action": (
            "Use a registered independent reviewer and a valid "
            "reviewed-work authorship declaration."
        ),
        "related_tools": ["dialectic", "request_review"],
    }


@dataclass(frozen=True)
class _ResolutionCommitResult:
    """Terminal-decision ownership after the BEAM/Python write attempt."""

    terminal_committed: bool
    commit_unknown: bool
    origin: str


@dataclass(frozen=True)
class _ResolutionEffectResult:
    """Outcome of claiming and applying a durable resolution effect."""

    state: str
    claimed: bool
    execution_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class _TranscriptAppendRefused(RuntimeError):
    """The durable session frontier no longer matches the loaded protocol."""


def _meta_value(meta: Any, key: str, default: Any = None) -> Any:
    """Read a field from metadata regardless of object-vs-dict representation."""
    if meta is None:
        return default
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)


def _agent_label(agent_id: Optional[str]) -> Optional[str]:
    """Best-effort friendly label for an agent UUID."""
    if not agent_id:
        return None
    meta = getattr(mcp_server, "agent_metadata", {}).get(agent_id)
    for key in ("label", "public_agent_id", "structured_id", "agent_id"):
        value = _meta_value(meta, key)
        if value:
            return str(value)
    return None


def _agent_status(agent_id: Optional[str]) -> Optional[str]:
    """Best-effort live status for an agent UUID."""
    if not agent_id:
        return None
    meta = getattr(mcp_server, "agent_metadata", {}).get(agent_id)
    status = _meta_value(meta, "status")
    return str(status) if status else None


def _agent_display(agent_id: Optional[str]) -> str:
    """Human-readable agent reference."""
    if not agent_id:
        return "unassigned"
    return _agent_label(agent_id) or agent_id


def _as_string_list(value: Any) -> List[str]:
    """Normalize string/list-ish caller input into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _reviewer_objection_stands_in_session_data(session_data: Dict[str, Any]) -> bool:
    """Mirror ``DialecticSession._reviewer_objection_stands`` for API dicts.

    The get-session fast path never reconstructs a ``DialecticSession``, so its
    actionability must derive the standing verdict from either dict messages or
    ``DialecticMessage`` objects. The last synthesis verdict not authored by the
    paused agent wins; this deliberately survives reviewer reassignment, just as
    the protocol guard does.
    """
    paused_agent_id = session_data.get("paused_agent_id") or session_data.get("paused_agent")
    reviewer_agent_id = session_data.get("reviewer_agent_id") or session_data.get("reviewer")
    self_review = bool(reviewer_agent_id and reviewer_agent_id == paused_agent_id)
    transcript = session_data.get("transcript") or session_data.get("messages") or []

    for message in reversed(transcript):
        if isinstance(message, dict):
            phase = message.get("phase") or message.get("message_type") or message.get("role")
            agent_id = message.get("agent_id")
            agrees = message.get("agrees")
        else:
            phase = getattr(message, "phase", None)
            agent_id = getattr(message, "agent_id", None)
            agrees = getattr(message, "agrees", None)

        if phase != "synthesis" or agrees is None:
            continue
        if not self_review and agent_id == paused_agent_id:
            continue

        if isinstance(agrees, bool):
            verdict = agrees
        elif isinstance(agrees, str):
            token = agrees.strip().lower()
            if token in {"true", "1", "yes"}:
                verdict = True
            elif token in {"false", "0", "no"}:
                verdict = False
            else:
                continue
        elif isinstance(agrees, (int, float)) and agrees in (0, 1):
            verdict = bool(agrees)
        else:
            continue
        return verdict is False

    return False


def _reviewer_verdict_pending_in_session_data(session_data: Dict[str, Any]) -> bool:
    """Mirror ``DialecticSession._reviewer_verdict_pending`` for fast reads."""
    paused_agent_id = session_data.get("paused_agent_id") or session_data.get("paused_agent")
    reviewer_agent_id = session_data.get("reviewer_agent_id") or session_data.get("reviewer")
    if not reviewer_agent_id or reviewer_agent_id == paused_agent_id:
        return False

    transcript = session_data.get("transcript") or session_data.get("messages") or []
    has_independent_antithesis = False
    has_independent_verdict = False
    for message in transcript:
        if isinstance(message, dict):
            phase = message.get("phase") or message.get("message_type") or message.get("role")
            agent_id = message.get("agent_id")
            agrees = message.get("agrees")
        else:
            phase = getattr(message, "phase", None)
            agent_id = getattr(message, "agent_id", None)
            agrees = getattr(message, "agrees", None)

        if agent_id == paused_agent_id:
            continue
        if phase == "antithesis":
            has_independent_antithesis = True
        elif phase == "synthesis" and agrees is not None:
            has_independent_verdict = True

    return has_independent_antithesis and not has_independent_verdict


def _latest_synthesis_agent_in_session_data(
    session_data: Dict[str, Any],
) -> Optional[str]:
    """Return the author of the latest synthesis message, if any."""
    transcript = session_data.get("transcript") or session_data.get("messages") or []
    for message in reversed(transcript):
        if isinstance(message, dict):
            phase = message.get("phase") or message.get("message_type") or message.get("role")
            agent_id = message.get("agent_id")
        else:
            phase = getattr(message, "phase", None)
            agent_id = getattr(message, "agent_id", None)
        if phase == "synthesis":
            return agent_id
    return None


def _saved_brief_thesis_call(session_id: str, session_type: Optional[str]) -> str:
    """Build a valid no-copy thesis call for review and recovery sessions."""
    condition_arg = (
        ", proposed_conditions=[...]"
        if str(session_type or "review").lower() == "recovery"
        else ""
    )
    return (
        f"dialectic(action='thesis', session_id='{session_id}', "
        f"use_brief_as_thesis=true{condition_arg})"
    )


def _last_activity_age_s(session_data: Dict[str, Any]) -> Optional[float]:
    """Seconds since the last transcript entry, or None if unreadable.

    None is returned rather than a default: "nothing has happened for 9 minutes"
    and "the clock could not be read" are different findings, and only one of
    them says anything about the reviewer.
    """
    transcript = session_data.get("transcript") or session_data.get("messages") or []
    newest = None
    for message in transcript:
        phase = (
            message.get("phase") or message.get("message_type") or message.get("role")
            if isinstance(message, dict)
            else getattr(message, "phase", None)
        )
        if phase in NON_PROTOCOL_PHASES:
            continue  # a consult is not activity on the review
        stamp = (
            message.get("timestamp")
            if isinstance(message, dict)
            else getattr(message, "timestamp", None)
        )
        if not stamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if newest is not None and parsed < newest:
            return None  # The transcript's causal order has no reliable clock.
        newest = parsed
    if newest is None:
        return None
    elapsed = (datetime.now(timezone.utc) - newest).total_seconds()
    return elapsed if elapsed >= 0 else None


def _has_orchestrated_reviewer_budget(session_data: Dict[str, Any], reviewer_id: str) -> bool:
    """Use recorded provenance, never assignment alone, to identify the runner.

    Some read shapes omit provenance. They remain unclassified rather than
    assigning the orchestrated runner's deadline to a human or unknown agent.
    """
    transcript = session_data.get("transcript") or session_data.get("messages") or []
    for message in reversed(transcript):
        agent_id = message.get("agent_id") if isinstance(message, dict) else getattr(message, "agent_id", None)
        if agent_id != reviewer_id:
            continue
        phase = (
            message.get("phase") or message.get("message_type") or message.get("role")
            if isinstance(message, dict)
            else getattr(message, "phase", None)
        )
        if phase in NON_PROTOCOL_PHASES:
            # A consult the reviewer files carries an external_consult stamp;
            # it must not reclassify the reviewer's own assignment.
            continue
        metrics = message.get("observed_metrics") if isinstance(message, dict) else getattr(message, "observed_metrics", None)
        if not isinstance(metrics, dict):
            continue
        backend = metrics.get("reviewer_backend")
        if isinstance(backend, dict) and "reviewer_kind" in backend:
            return backend.get("reviewer_kind") == "orchestrated"
    # The runner registers this fingerprint at onboard (_reviewer_model_type),
    # including on older messages whose backend stamp predates reviewer_kind.
    meta = getattr(mcp_server, "agent_metadata", {}).get(reviewer_id)
    model_type = _meta_value(meta, "model_type")
    return isinstance(model_type, str) and model_type.startswith("dialectic_reviewer:")


def _build_dialectic_actionability(session_data: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate a session payload with concrete next-action metadata."""
    # Two dict shapes reach this function. `load_session_as_dict`
    # (dialectic/session.py) and `list_all_sessions` emit "paused_agent";
    # `DialecticSession.to_dict` emits "paused_agent_id". Only the reviewer key
    # had the fallback, so on the default `get` fast path paused_agent_id was
    # always None — which rendered `allowed_agent_ids: []` and "Paused agent
    # 'unassigned' should submit thesis" on sessions that had a perfectly good
    # paused agent (#1414).
    paused_agent_id = session_data.get("paused_agent_id") or session_data.get("paused_agent")
    if paused_agent_id == "unknown":  # session.py sentinel for a NULL column
        paused_agent_id = None
    reviewer_agent_id = session_data.get("reviewer_agent_id") or session_data.get("reviewer")
    reviewed_work_authorship = read_reviewed_work_authorship(session_data)
    reviewer_author_conflict = reviewed_work_authorship.recuses(
        reviewer_agent_id, paused_agent_id=paused_agent_id
    )
    phase = str(session_data.get("phase") or "").lower()
    session_id = session_data.get("session_id") or "<session_id>"
    session_type = session_data.get("session_type") or "review"
    awaiting_facilitation = bool(session_data.get("awaiting_facilitation", False))
    resolution_effect_state = session_data.get("resolution_effect_state")
    effect_needs_reconciliation = bool(
        phase == "resolved"
        and resolution_effect_state in {
            "pending", "applying", "unknown", "unknown_partial"
        }
    )
    reviewer_objection_stands = (
        phase == "synthesis"
        and _reviewer_objection_stands_in_session_data(session_data)
    )
    reviewer_verdict_pending = (
        phase == "synthesis"
        and _reviewer_verdict_pending_in_session_data(session_data)
    )
    independent_reviewer_can_revise = bool(
        reviewer_agent_id
        and reviewer_agent_id != paused_agent_id
        and not reviewer_author_conflict
    )
    # A self-reviewed session that has been routed to facilitation: the paused
    # agent's own agreeing synthesis was recorded and refused as non-authorizing
    # (#1585 item 1). Without this the guidance below keeps telling that agent to
    # "negotiate until convergence" against itself, and every attempt is refused.
    self_review_awaiting_ratification = bool(
        reviewer_agent_id
        and reviewer_agent_id == paused_agent_id
        and awaiting_facilitation
    )
    latest_synthesis_agent_id = _latest_synthesis_agent_in_session_data(session_data)
    paused_response_owed = bool(
        reviewer_objection_stands
        and independent_reviewer_can_revise
        and latest_synthesis_agent_id != paused_agent_id
    )
    reviewer_reconsideration_owed = bool(
        reviewer_objection_stands
        and independent_reviewer_can_revise
        and latest_synthesis_agent_id == paused_agent_id
    )

    try:
        from ..context import get_context_agent_id
        current_agent_id = get_context_agent_id()
    except Exception:
        current_agent_id = None
    try:
        from ..identity.operator import is_operator_caller
        operator_caller = is_operator_caller()
    except Exception:
        operator_caller = False

    current_agent_role = None
    if operator_caller:
        current_agent_role = "operator"
    elif current_agent_id:
        if current_agent_id == paused_agent_id:
            current_agent_role = "paused_agent"
        elif reviewed_work_authorship.recuses(
            current_agent_id, paused_agent_id=paused_agent_id
        ):
            current_agent_role = "reviewed_work_author"
        elif reviewer_agent_id and current_agent_id == reviewer_agent_id:
            current_agent_role = "reviewer"
        else:
            current_agent_role = "observer"

    allowed_agent_ids: List[str] = []
    required_role = "observer"
    required_agent_id = None
    required_agent_label = None
    recommended_action = "Inspect the session transcript."

    if effect_needs_reconciliation:
        required_role = "operator"
        allowed_agent_ids = (
            [current_agent_id]
            if operator_caller and current_agent_id
            else []
        )
        recommended_action = (
            "The decision is terminal, but its runtime effect is not confirmed "
            "applied. An operator should inspect and reconcile the effect; "
            "applying/unknown_partial states must not be replayed automatically."
        )
    elif phase == "thesis":
        required_role = "paused_agent"
        required_agent_id = paused_agent_id
        required_agent_label = _agent_label(paused_agent_id)
        allowed_agent_ids = [paused_agent_id] if paused_agent_id else []
        recommended_action = (
            f"Paused agent '{_agent_display(paused_agent_id)}' should submit thesis."
        )
    elif phase == "antithesis":
        required_role = "reviewer"
        required_agent_id = reviewer_agent_id
        required_agent_label = _agent_label(reviewer_agent_id)
        if reviewer_agent_id and reviewer_author_conflict:
            required_role = "reviewer_or_operator"
            allowed_agent_ids = []
            recommended_action = (
                "The assigned reviewer is recused from independent review because "
                "it is a declared author of the reviewed work (or the declaration "
                "is malformed). Reassign an independent reviewer."
            )
        elif reviewer_agent_id:
            allowed_agent_ids = [reviewer_agent_id]
            recommended_action = (
                f"Reviewer '{_agent_display(reviewer_agent_id)}' should submit antithesis."
            )
            if current_agent_id and current_agent_id not in {paused_agent_id, reviewer_agent_id}:
                recommended_action += (
                    " If the operator wants this bound agent to answer instead, retry antithesis with "
                    "`take_over_if_requested=true` or use `dialectic(action='reassign')`."
                )
        else:
            if (
                current_agent_id
                and current_agent_id != paused_agent_id
                and not reviewed_work_authorship.recuses(
                    current_agent_id, paused_agent_id=paused_agent_id
                )
            ):
                allowed_agent_ids = [current_agent_id]
            if reviewed_work_authorship.is_invalid:
                recommended_action = (
                    "Reviewed-work authorship metadata is malformed. The reviewer "
                    "slot fails closed until an operator repairs or replaces the session."
                )
            else:
                recommended_action = (
                    "Any eligible reviewer who is not a declared work author may "
                    "claim this session by submitting antithesis."
                )
    elif phase == "synthesis":
        if reviewer_author_conflict:
            required_role = "reviewer_or_operator"
            required_agent_id = reviewer_agent_id
            required_agent_label = _agent_label(reviewer_agent_id)
            allowed_agent_ids = []
            recommended_action = (
                "The assigned reviewer is recused from independent review. Reassign "
                "an independent reviewer before synthesis can resolve this session."
            )
        elif reviewer_verdict_pending:
            required_role = "reviewer"
            required_agent_id = reviewer_agent_id
            required_agent_label = _agent_label(reviewer_agent_id)
            allowed_agent_ids = [reviewer_agent_id] if reviewer_agent_id else []
            recommended_action = (
                "The independent reviewer has submitted antithesis and now owes "
                "the first synthesis verdict. The paused agent may not agree to "
                "resume until that verdict is recorded."
            )
        elif reviewer_objection_stands and paused_response_owed:
            required_role = "paused_agent"
            required_agent_id = paused_agent_id
            required_agent_label = _agent_label(paused_agent_id)
            allowed_agent_ids = [paused_agent_id] if paused_agent_id else []
            recommended_action = (
                "The reviewer's rejection stands, so the paused agent should respond "
                "once with revised evidence or conditions. An agreeing response is "
                "recorded but cannot resume the agent until the reviewer independently "
                "ratifies it."
            )
        elif reviewer_objection_stands and reviewer_reconsideration_owed:
            required_role = "reviewer"
            required_agent_id = reviewer_agent_id
            required_agent_label = _agent_label(reviewer_agent_id)
            allowed_agent_ids = [reviewer_agent_id] if reviewer_agent_id else []
            recommended_action = (
                "The paused agent has answered the standing rejection. The same "
                "independent reviewer should evaluate that response and submit a later "
                "synthesis that maintains or revises the verdict."
            )
        elif reviewer_objection_stands:
            required_role = "reviewer_or_facilitator"
            recommended_action = (
                "This session has no independent reviewer who can reconsider the "
                "standing rejection. An operator should assign a facilitator or "
                "replacement reviewer."
            )
        elif self_review_awaiting_ratification:
            required_role = "reviewer_or_operator"
            required_agent_id = None
            required_agent_label = None
            allowed_agent_ids = [current_agent_id] if operator_caller and current_agent_id else []
            recommended_action = (
                "This session is self-reviewed: the reviewer and the paused agent are "
                "the same identity, so no independent verdict can be reached from "
                "inside it. The synthesis is recorded but cannot lift the pause. An "
                "operator should assign an independent reviewer."
            )
        else:
            required_role = "participant"
            required_agent_id = None
            required_agent_label = None
            allowed_agent_ids = [
                agent_id for agent_id in [paused_agent_id, reviewer_agent_id] if agent_id
            ]
            recommended_action = (
                "Paused agent and reviewer should negotiate via dialectic(action='synthesis') until convergence."
            )
    elif phase == "failed" and awaiting_facilitation:
        required_role = "reviewer_or_operator"
        required_agent_id = reviewer_agent_id
        required_agent_label = _agent_label(reviewer_agent_id)
        allowed_agent_ids = (
            [reviewer_agent_id]
            if reviewer_agent_id and not reviewer_author_conflict
            else []
        )
        if operator_caller and current_agent_id:
            allowed_agent_ids.append(current_agent_id)
        if reviewer_author_conflict:
            recommended_action = (
                "This failed session has a recused reviewer and an unanswered "
                "facilitation request. Only an operator may assign an independent "
                "replacement; reassignment revives it."
            )
        else:
            recommended_action = (
                "This failed session still carries an unanswered facilitation request. "
                "An operator should assign a replacement reviewer, or the current "
                "independent reviewer may hand the session off; reassignment revives it."
            )
    elif phase in {"resolved", "failed", "escalated", "quorum_voting"}:
        required_role = "none"
        recommended_action = (
            "No direct write step is pending. Review the transcript or follow the recorded resolution."
        )

    current_agent_can_submit: Optional[bool]
    if effect_needs_reconciliation:
        current_agent_can_submit = operator_caller
    elif current_agent_id is None:
        current_agent_can_submit = None
    elif phase == "antithesis" and reviewer_agent_id is None:
        current_agent_can_submit = bool(
            current_agent_id != paused_agent_id
            and not reviewed_work_authorship.recuses(
                current_agent_id, paused_agent_id=paused_agent_id
            )
        )
    elif phase == "failed" and awaiting_facilitation:
        current_agent_can_submit = operator_caller or (
            current_agent_id == reviewer_agent_id
            and current_agent_id != paused_agent_id
            and not reviewer_author_conflict
        )
    else:
        current_agent_can_submit = current_agent_id in allowed_agent_ids

    # Plain-language "whose move is it" — the protocol fields above are
    # accurate but easy to misread as a hang (a live session waiting on the
    # CALLER's synthesis was read as "stalled" by two experienced operators,
    # 2026-07-28). whose_move answers from the caller's seat; next_call is a
    # ready-to-use template when the move is theirs.
    # How long this has been waiting, and whether that is yet odd. whose_move
    # answers WHO; without this it never answered WHEN, and the open-slot text
    # read identically at 72 seconds and 72 minutes (see wait_assessment).
    reviewer_owed = (
        phase in {"antithesis", "synthesis"}
        and str(session_data.get("status") or "").lower() not in {
            "resolved", "failed", "escalated", "timeout", "abandoned",
        }
        and required_role == "reviewer"
        and independent_reviewer_can_revise
        and current_agent_role != "reviewer"
    )
    awaiting_kind = (
        ("reconsideration" if reviewer_reconsideration_owed else "verdict")
        if reviewer_owed else None
    )
    wait = assess_wait(
        elapsed_s=_last_activity_age_s(session_data) if reviewer_owed else None,
        awaiting=awaiting_kind,
        orchestrated=bool(reviewer_owed and _has_orchestrated_reviewer_budget(session_data, reviewer_agent_id)),
    )
    facilitation_supported = suggests_facilitation(wait["assessment"])

    whose_move = "nobody — session is terminal"
    next_call: Optional[str] = None
    if effect_needs_reconciliation:
        reconcile_call = (
            f"dialectic(action='reconcile_effect', session_id='{session_id}')"
        )
        if current_agent_role == "operator":
            whose_move = "YOURS — inspect and reconcile the committed runtime effect"
            next_call = reconcile_call
        else:
            whose_move = "an operator's — the terminal runtime effect needs reconciliation"
    elif phase == "thesis":
        if current_agent_role == "paused_agent":
            whose_move = "YOURS — your thesis is owed; the saved brief can be reused"
            next_call = _saved_brief_thesis_call(session_id, session_type)
        else:
            whose_move = "the paused agent's — their thesis is owed"
    elif phase == "antithesis":
        if reviewer_author_conflict:
            if current_agent_role == "operator":
                whose_move = "YOURS — reassign the recused reviewer"
                next_call = (
                    f"dialectic(action='reassign', session_id='{session_id}', "
                    "reason='Assigned reviewer authored the reviewed work')"
                )
            elif current_agent_id == reviewer_agent_id:
                whose_move = (
                    "NOT YOURS — you are recused as an author of the reviewed work; "
                    "hand off or ask an operator to reassign"
                )
            else:
                whose_move = "an operator's — the recused reviewer must be reassigned"
        elif reviewer_agent_id is None:
            if current_agent_role == "paused_agent":
                whose_move = (
                    "a reviewer's — the slot is open; no identified reviewer "
                    "obligation or wait budget is established"
                )
            elif current_agent_role == "reviewed_work_author":
                whose_move = (
                    "NOT YOURS — you are a declared author of the reviewed work "
                    "and cannot claim its independent-review slot"
                )
            elif reviewed_work_authorship.is_invalid:
                whose_move = (
                    "an operator's — malformed authorship metadata blocks reviewer assignment"
                )
            else:
                whose_move = "a reviewer's — the slot is OPEN, you may claim it"
                next_call = (
                    f"dialectic(action='antithesis', session_id='{session_id}', "
                    "reasoning='...', concerns=[...])"
                )
        elif current_agent_role == "reviewer":
            whose_move = "YOURS — your antithesis is owed"
            next_call = (
                f"dialectic(action='antithesis', session_id='{session_id}', "
                "reasoning='...', concerns=[...])"
            )
        else:
            whose_move = "the reviewer's — their antithesis is owed"
    elif phase == "synthesis" and reviewer_author_conflict:
        if current_agent_role == "operator":
            whose_move = "YOURS — reassign the recused reviewer before synthesis"
            next_call = (
                f"dialectic(action='reassign', session_id='{session_id}', "
                "reason='Assigned reviewer authored the reviewed work')"
            )
        elif current_agent_id == reviewer_agent_id:
            whose_move = (
                "NOT YOURS — your authorship conflict prevents an independent verdict"
            )
        else:
            whose_move = "an operator's — an independent reviewer must be assigned"
    elif phase == "synthesis" and reviewer_verdict_pending:
        if current_agent_role == "reviewer":
            whose_move = "YOURS — your first synthesis verdict is owed"
            next_call = (
                f"dialectic(action='synthesis', session_id='{session_id}', "
                "agrees=true/false, root_cause='...', reasoning='...', "
                "proposed_conditions=[...])"
            )
        elif current_agent_role == "paused_agent":
            whose_move = (
                "NOT YOURS — the independent reviewer's synthesis verdict is pending"
            )
        else:
            whose_move = "the reviewer's — their first synthesis verdict is owed"
    elif phase == "synthesis" and reviewer_objection_stands and paused_response_owed:
        if current_agent_role == "paused_agent":
            whose_move = "YOURS — respond once to the reviewer's standing rejection"
            next_call = (
                f"dialectic(action='synthesis', session_id='{session_id}', "
                "agrees=true/false, root_cause='...', reasoning='...', "
                "proposed_conditions=[...])"
            )
        elif current_agent_role == "reviewer":
            whose_move = "NOT YOURS — wait for the paused agent's response"
        else:
            whose_move = "the paused agent's — their response to the rejection is owed"
    elif (
        phase == "synthesis"
        and reviewer_objection_stands
        and reviewer_reconsideration_owed
    ):
        if current_agent_role == "reviewer":
            whose_move = "YOURS — review the new evidence and maintain or revise your verdict"
            next_call = (
                f"dialectic(action='synthesis', session_id='{session_id}', "
                "agrees=true/false, root_cause='...', reasoning='...', "
                "proposed_conditions=[...])"
            )
        elif current_agent_role == "paused_agent":
            if facilitation_supported:
                whose_move = (
                    "NOT YOURS — the reviewer's rejection stands; wait for the reviewer "
                    "or ask an operator to reassign/facilitate"
                )
            else:
                whose_move = (
                    "NOT YOURS — the reviewer's reconsideration is pending; wait for "
                    "the reviewer and read wait_assessment.note before intervening"
                )
        elif current_agent_role == "operator":
            if facilitation_supported:
                whose_move = "YOURS — reassign/facilitate unless the reviewer will revise"
                next_call = (
                    f"dialectic(action='reassign', session_id='{session_id}', "
                    "reason='Facilitate standing reviewer rejection')"
                )
            else:
                whose_move = (
                    "the reviewer's — their reconsideration is pending; read "
                    "wait_assessment.note before intervening"
                )
        else:
            if facilitation_supported:
                whose_move = (
                    "an operator's — reassign/facilitate unless the reviewer will revise its verdict"
                )
            else:
                whose_move = (
                    "the reviewer's — their reconsideration is pending; read "
                    "wait_assessment.note before intervening"
                )
    elif phase == "synthesis" and reviewer_objection_stands:
        if current_agent_role == "operator":
            whose_move = "YOURS — assign an independent reviewer/facilitator"
            next_call = (
                f"dialectic(action='reassign', session_id='{session_id}', "
                "reason='Facilitate standing reviewer rejection')"
            )
        elif current_agent_role == "paused_agent":
            whose_move = "NOT YOURS — an independent facilitator is required"
        else:
            whose_move = "an operator's — an independent facilitator is required"
    elif phase == "synthesis" and self_review_awaiting_ratification:
        if current_agent_role == "operator":
            whose_move = "YOURS — assign an independent reviewer; this session reviewed itself"
            next_call = (
                f"dialectic(action='reassign', session_id='{session_id}', "
                "reason='Self-review cannot ratify its own resumption')"
            )
        elif current_agent_role == "paused_agent":
            whose_move = (
                "NOT YOURS — you are also the reviewer here, so your agreement cannot "
                "lift your own pause; an independent reviewer or operator is required"
            )
        else:
            whose_move = "an operator's — an independent reviewer is required"
    elif phase == "synthesis":
        if current_agent_role in {"paused_agent", "reviewer"}:
            whose_move = "YOURS — a converging synthesis is owed (negotiate until agreement)"
            next_call = (
                f"dialectic(action='synthesis', session_id='{session_id}', "
                "agrees=true/false, root_cause='...', reasoning='...', "
                "proposed_conditions=[...])"
            )
        else:
            whose_move = "the participants' — they negotiate synthesis until convergence"
    elif phase == "failed" and awaiting_facilitation:
        reassign_call = (
            f"dialectic(action='reassign', session_id='{session_id}', "
            "reason='Answer outstanding facilitation request')"
        )
        if current_agent_role == "operator":
            whose_move = "YOURS — reassign a reviewer to revive this session"
            next_call = reassign_call
        elif (
            current_agent_role == "reviewer"
            and reviewer_agent_id != paused_agent_id
            and not reviewer_author_conflict
        ):
            whose_move = "YOURS — hand this session to a replacement reviewer"
            next_call = reassign_call
        elif current_agent_role == "paused_agent":
            whose_move = (
                "NOT YOURS — wait for the reviewer or an operator to reassign/facilitate"
            )
        elif reviewer_author_conflict:
            whose_move = "an operator's — the recused reviewer must be replaced"
        else:
            whose_move = (
                "an operator's or the current reviewer's — reassignment revives this session"
            )

    return {
        "whose_move": whose_move,
        "next_call": next_call,
        "paused_agent_label": _agent_label(paused_agent_id),
        "reviewer_label": _agent_label(reviewer_agent_id),
        "reviewer_status": _agent_status(reviewer_agent_id),
        "required_role": required_role,
        "required_agent_id": required_agent_id,
        "required_agent_label": required_agent_label,
        "allowed_agent_ids": allowed_agent_ids,
        "current_agent_id": current_agent_id,
        "current_agent_role": current_agent_role,
        "current_agent_can_submit": current_agent_can_submit,
        "reviewer_verdict_pending": reviewer_verdict_pending,
        "reviewed_work_authorship": reviewed_work_authorship.as_dict(),
        "reviewer_author_conflict": reviewer_author_conflict,
        "resolution_effect_state": resolution_effect_state,
        "recommended_action": recommended_action,
        "wait_assessment": wait,
    }


def _reviewer_authorship_conflict(
    session: DialecticSession, candidate_id: Optional[str]
) -> Optional[tuple[str, str]]:
    """Return a typed reason when a candidate cannot be independent."""
    declaration = getattr(
        session,
        "reviewed_work_authorship",
        read_reviewed_work_authorship(
            getattr(session, "paused_agent_state", {})
        ),
    )
    if candidate_id == session.paused_agent_id:
        # Explicit self-review has its own non-independent semantics and never
        # counts as a peer review.
        return None
    if declaration.is_invalid:
        return (
            "REVIEW_AUTHORSHIP_METADATA_INVALID",
            "Reviewed-work authorship metadata is malformed; reviewer assignment fails closed.",
        )
    if declaration.recuses(
        candidate_id, paused_agent_id=session.paused_agent_id
    ):
        return (
            "REVIEWER_AUTHOR_CONFLICT",
            "A declared author of the reviewed work cannot serve as its independent reviewer.",
        )
    return None


_PAUSE_EVIDENCE_SCHEMA = "dialectic.pause_evidence.v1"


_PAUSE_EISV_KEYS = ("E", "I", "S", "V")


_PAUSE_SOURCE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def _pause_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _project_pause_lane(
    lane: Any,
    *,
    fallback_source: str,
    fallback_values: Any,
) -> Dict[str, Any]:
    lane = lane if isinstance(lane, dict) else {}
    fallback_source = str(fallback_source)
    if _PAUSE_SOURCE_PATTERN.fullmatch(fallback_source) is None:
        fallback_source = "unknown"
    source = lane.get("source")
    if not isinstance(source, str) or _PAUSE_SOURCE_PATTERN.fullmatch(source) is None:
        source = fallback_source
    values = lane.get("values")
    values = values if isinstance(values, dict) else fallback_values
    values = values if isinstance(values, dict) else {}
    return {
        "source": source,
        "values": {key: _pause_number(values.get(key)) for key in _PAUSE_EISV_KEYS},
    }


def _project_pause_measurement(
    measurement: Any,
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep only decision-relevant numeric lanes for an external reviewer."""
    measurement = measurement if isinstance(measurement, dict) else {}
    return {
        "primary": _project_pause_lane(
            measurement.get("primary"),
            fallback_source=metrics.get("primary_eisv_source") or "unknown",
            fallback_values=metrics,
        ),
        "ode": _project_pause_lane(
            measurement.get("ode"),
            fallback_source="ode_diagnostic",
            fallback_values=metrics.get("ode"),
        ),
    }


def _capture_pause_evidence(monitor: Any) -> Dict[str, Any]:
    """Return a bounded last-decision bundle for dialectic review.

    ``GovernanceState.to_dict()`` is the diagnostic ODE state. It does not carry
    the verdict-authoritative behavioral vector, final risk/verdict provenance,
    policy decision, or enforcement request. A reviewer must not be invited to
    treat that partial diagnostic state as the basis of a pause.

    The monitor retains the actual result object from its last completed update.
    Later update phases enrich that same object with the persisted EISV telemetry
    envelope, so prefer the envelope when present and fall back to the result's
    already-final metrics/policy fields. If no completed update is available, say
    so explicitly rather than manufacturing authoritative-looking evidence.
    """
    unavailable = {
        "schema": _PAUSE_EVIDENCE_SCHEMA,
        "evidence_status": "unavailable",
        "source": "live_monitor",
        "selection": "last_completed_governance_update_at_session_open",
    }
    if monitor is None:
        return {
            **unavailable,
            "source": "none",
            "limitation": "No live governance monitor was available when the session opened.",
        }

    result = getattr(monitor, "_last_governance_result", None)
    if not isinstance(result, dict):
        return {
            **unavailable,
            "limitation": (
                "The live monitor had no completed governance update retained; "
                "diagnostic ODE state was intentionally not substituted."
            ),
        }

    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    telemetry = (
        result.get("eisv_telemetry")
        if isinstance(result.get("eisv_telemetry"), dict)
        else {}
    )
    measurement = _project_pause_measurement(
        telemetry.get("measurement"),
        metrics,
    )
    policy_evaluation = (
        telemetry.get("policy_evaluation")
        if isinstance(telemetry.get("policy_evaluation"), dict)
        else result.get("policy_evaluation")
    )
    enforcement = (
        telemetry.get("enforcement")
        if isinstance(telemetry.get("enforcement"), dict)
        else result.get("enforcement")
    )

    evidence = {
        "schema": _PAUSE_EVIDENCE_SCHEMA,
        "evidence_status": "available",
        "source": "last_completed_governance_update",
        "selection": "last_completed_governance_update_at_session_open",
        "observed_at": telemetry.get("observed_at") or result.get("timestamp"),
        "measurement_id": telemetry.get("measurement_id"),
        "measurement": measurement,
        "policy_evaluation": policy_evaluation if isinstance(policy_evaluation, dict) else {},
        "enforcement": enforcement if isinstance(enforcement, dict) else {},
    }
    if isinstance(result.get("risk_attribution"), dict):
        evidence["risk_attribution"] = result["risk_attribution"]
    return evidence


class AutomatedTrigger(str):
    """A ``trigger_source`` that only code in this process can supply.

    ``core.dialectic_sessions.trigger_source`` used to be guessed from
    substrings of the free-text ``reason``: "auto-recovery" or "auto-triggered"
    meant ``circuit_breaker``, "loop" meant ``loop_detection``. Both live
    ``loop_detection`` rows were agents writing "loop" in a review request
    (56bead4ed32ab6a5, "self-governance loop"; 755fe9368bb1c920, a PR
    review). Taking the value from an argument
    instead would not help: the params middleware passes unknown keys
    through, so any caller could claim to be an automated trigger.

    JSON cannot produce an instance of this class, so a value that arrives
    through the MCP tool is never one. An in-process trigger passes
    ``trigger_source=AutomatedTrigger("circuit_breaker")``; every other
    request is recorded ``manual``, meaning only that a caller asked through
    the tool. ``manual`` partitions nothing further: probe and organic
    traffic both carry it, and they are separated by agent label
    (``src/dialectic_outcomes.py``).
    """


LIST_TOPIC_PREVIEW_CHARS = 280


def _preview_topic(session: Dict[str, Any]) -> None:
    """Shorten a list row's topic in place, saying so when it does."""
    topic = session.get("topic")
    if isinstance(topic, str) and len(topic) > LIST_TOPIC_PREVIEW_CHARS:
        session["topic"] = topic[:LIST_TOPIC_PREVIEW_CHARS].rstrip() + "…"
        session["topic_truncated"] = True
        session["topic_chars"] = len(topic)


SYNTHETIC_REVIEWER_ID = "llm-synthetic-reviewer"


def _judgment_was_formed(value: Any) -> bool:
    """Resolve the abstention flag with a fail-closed raw-handler contract.

    The schema validator already maps unknown strings to ``False``, but these
    handlers are also called directly by tests and by compatibility paths that
    can bypass schema validation.  Only an omitted/``None`` value preserves the
    backwards-compatible default of ``True``; a malformed supplied value must
    not silently file a verdict.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, bool):
        return value
    return False


_REVIEWER_PROVENANCE_KEYS = (
    "backend",
    "host_id",
    "model_requested",
    "model_used",
    "models_used",
    "tokens_used",
    "cost_usd",
    "latency_ms",
    "finish_reason",
    "fallback_from",
    "warnings",
    "source",
    "structured",
    "note",
    "consult_source",
    "consulted_at",
    # The orchestrated reviewer's host list (docs/proposals/active/
    # dialectic-reviewer-hosts-v0.md 2.6): which list a verdict was produced
    # under, every host tried, and whether the answering host may approve.
    "host_list",
    "host_config_digest",
    "attempts",
    "vouched",
    "vouched_by",
    "authorized_by",
    "answered_family",
)


_REVIEWER_KINDS = {
    "in_process_synthetic",
    "orchestrated",
    "agent_submitted",
    "external_consult",
}


_PROVENANCE_STR_MAX = 200


def _reviewer_provenance_stamp(
    supplied: Optional[Dict[str, Any]],
    *,
    kind: str,
    degraded: bool = False,
) -> Dict[str, Any]:
    """Build the persisted reviewer_backend stamp. Descriptive provenance,
    not identity proof — same posture as provenance_context on check-ins."""
    stamp: Dict[str, Any] = {}
    if isinstance(supplied, dict):
        for key in _REVIEWER_PROVENANCE_KEYS:
            value = supplied.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value[:_PROVENANCE_STR_MAX]
            stamp[key] = value
    stamp["reviewer_kind"] = kind if kind in _REVIEWER_KINDS else "agent_submitted"
    stamp["degraded"] = bool(degraded)
    return stamp


def _synthetic_reviewer_provenance(llm_result: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp for verdicts produced by the in-process synthetic reviewer."""
    from ..support.inference_registry import default_local_model

    return _reviewer_provenance_stamp(
        {
            "backend": "local_ollama",
            "model_used": default_local_model() or None,
            "source": llm_result.get("source"),
            "structured": llm_result.get("_structured"),
            "note": llm_result.get("_note"),
        },
        kind="in_process_synthetic",
        degraded=bool(llm_result.get("_degraded")),
    )


def _merge_caller_reviewer_provenance(
    observed_metrics: Optional[Dict[str, Any]],
    reviewer_provenance: Any,
) -> Dict[str, Any]:
    """Fold an explicit reviewer_provenance argument into observed_metrics.

    An orchestrated reviewer that already writes observed_metrics["reviewer_backend"]
    directly is passed through untouched; an explicit reviewer_provenance argument
    wins over both. This is how an agent files a verdict it obtained OUTSIDE the
    server (a Codex/other-model consult) as a governed record:
    reviewer_provenance={"reviewer_kind": "external_consult", "backend": "codex-cli",
    "model_used": ..., "consult_source": ...}.
    """
    merged = dict(observed_metrics or {})
    if isinstance(reviewer_provenance, dict) and reviewer_provenance:
        kind = str(reviewer_provenance.get("reviewer_kind", "agent_submitted"))
        merged["reviewer_backend"] = _reviewer_provenance_stamp(
            reviewer_provenance,
            kind=kind,
            degraded=bool(reviewer_provenance.get("degraded")),
        )
    return merged


CONSULT_PHASE = "consult"


MAX_CONSULTS_PER_SESSION = 8


def _consult_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _synthetic_review_approves(
    synthesis: Dict[str, Any],
    antithesis: Optional[Dict[str, Any]] = None,
) -> bool:
    """Map the synthetic reviewer's model verdict to a BINDING agree/disagree.

    The synthetic reviewer emits TWO independent signals: the antithesis carries a
    `position` (agree / dispute / refine) and the synthesis carries a
    `recommendation` (RESUME / COOLDOWN / ESCALATE). Approval requires BOTH to
    point at resume:

    - recommendation must be RESUME (COOLDOWN/ESCALATE are non-approval), AND
    - the antithesis must not be a `dispute`.

    The second clause is load-bearing. The two signals come from separate model
    calls and can disagree: live on 2026-06-23, after #1015 bound the
    recommendation, a transparently-unsafe thesis ("disable the risk check and
    resume with no conditions") was correctly `position=dispute`-d by the reviewer
    yet the synthesis still recommended RESUME-with-conditions, so binding on the
    recommendation alone STILL auto-resolved RESUME — the rubber-stamp persisted.
    A disputed antithesis is a genuine rejection signal; honoring the operator's
    fail-closed choice it must not auto-resolve into a resume. The recorded
    antithesis/synthesis/conditions are preserved and the session falls through to
    awaiting_facilitation for a human/peer to confirm.

    A missing/unparseable recommendation defaults to non-approval (don't approve
    without a real verdict). The fully-degraded case (generate_synthesis returns
    None) is handled upstream — the session stays open rather than fabricating one.
    """
    rec = str((synthesis or {}).get("recommendation", "")).upper().strip()
    if rec != "RESUME":
        return False
    position = str((antithesis or {}).get("position", "")).lower().strip()
    if position == "dispute":
        # Reviewer disputed; a RESUME synthesis over a dispute is internally
        # inconsistent — fail closed rather than rubber-stamp.
        return False
    return True


_REVIEWER_FLOOR_NAMES = frozenset({"local", "ollama", "ollama:local"})


def _reviewer_fallback_block() -> tuple[str, str]:
    """Why the in-process synthetic reviewer may not stand in, as (reason, detail),
    or ("", "") when it may.

    Mirrors the reviewer's own reading of the server's selection
    (orchestrator_dispatch._reviewer_host_selection, then
    host_list.reviewer_host_plan): with a resolution error, or with any listed
    host, only a listed host's verdict may approve, and the local reviewer is
    the floor, which may not. So a dispatch failure must leave the session
    awaiting facilitation rather than hand approval to the local reviewer.
    Never raises: an unexpected failure blocks, so it fails closed."""
    try:
        from .orchestrator_dispatch import REVIEWER_HOSTS_ERROR_ENV, _reviewer_host_selection

        selection = _reviewer_host_selection()
    except Exception as exc:  # pragma: no cover - defensive
        return "reviewer_hosts_unresolved", f"reviewer host selection failed ({type(exc).__name__})"
    error = selection.get(REVIEWER_HOSTS_ERROR_ENV, "").strip()
    if error:
        return "reviewer_hosts_unresolved", error
    listed = selection.get("UNITARES_DIALECTIC_REVIEWER_HOSTS", "").strip()
    if not listed:
        legacy = selection.get("UNITARES_DIALECTIC_REVIEWER_HOST", "").strip()
        if legacy.lower() not in _REVIEWER_FLOOR_NAMES:
            listed = legacy
    if listed:
        return "reviewer_hosts_listed", listed
    return "", ""


def _reviewer_profile_block(review_path_failure_reason: str) -> tuple[str, str]:
    """Why the in-process reviewer may not stand in for the reviewer's launch
    profile, as (reason, profile name), or ("", "") when it may.

    With orchestrated review and UNITARES_ORCHESTRATOR_PROFILES=1, the
    reviewer runs only as the dialectic.reviewer profile, and a refused spawn
    is the result (inference-hosts-config-v0 §2.5): the in-process reviewer
    could approve through a model the profile would not, and anyone able to
    make the spawn fail (its hourly limit, say) could steer reviews to it.
    Reached only when no orchestrated reviewer took the slot."""
    from .orchestrator_dispatch import (
        REVIEWER_PROFILE,
        orchestrated_review_enabled,
        reviewer_profiles_enabled,
    )

    if not (orchestrated_review_enabled() and reviewer_profiles_enabled()):
        return "", ""
    if review_path_failure_reason == "reviewer_crashed_fast":
        return "reviewer_profile_exited", REVIEWER_PROFILE
    return "reviewer_profile_refused", REVIEWER_PROFILE


def _synthetic_reviewer_enabled() -> bool:
    """Whether submit_thesis auto-completes a no-live-reviewer session via the
    local synthetic reviewer instead of leaving it to hang at awaiting_facilitation.

    ON by default — this is the one path that drives a dialectic from thesis to a
    resolved synthesis end-to-end without depending on a second live agent ever
    showing up (the historical failure mode: auto-select is disabled, no peer
    claims the slot, the session sits stuck for hours). The reviewer is a local
    model (gemma4) heterogeneous to Claude, so it satisfies the independence
    requirement the 2026-06-02 dialectic review set: a genuine antithesis, not a
    self-review at one remove. Set UNITARES_DIALECTIC_SYNTHETIC_REVIEWER=0 to fall
    back to pure peer/manual review (request leaves the slot open as before).
    """
    return os.environ.get(
        "UNITARES_DIALECTIC_SYNTHETIC_REVIEWER", "1"
    ).lower() in ("1", "true", "yes", "on")


def _snapshot_agent_state(agent_uuid: str) -> Optional[Dict[str, Any]]:
    """Compact authoritative evidence for the legacy synthetic-review prompt."""
    try:
        monitor = getattr(mcp_server, "monitors", {}).get(agent_uuid)
        evidence = _capture_pause_evidence(monitor)
        if evidence.get("evidence_status") != "available":
            return None
        measurement = evidence.get("measurement") or {}
        primary = measurement.get("primary") or {}
        values = primary.get("values") or {}
        policy = evidence.get("policy_evaluation") or {}
        inputs = policy.get("inputs") or {}
        return {
            "E": values.get("E"),
            "I": values.get("I"),
            "S": values.get("S"),
            "V": values.get("V"),
            "primary_eisv_source": primary.get("source"),
            "coherence": inputs.get("coherence"),
            "risk_score": inputs.get("risk_score"),
            "phi": inputs.get("phi"),
            "verdict": inputs.get("verdict"),
            "verdict_source": inputs.get("verdict_source"),
            "action": policy.get("action"),
            "sub_action": policy.get("sub_action"),
            "ode": (measurement.get("ode") or {}).get("values"),
        }
    except Exception:
        return None


@dataclass(frozen=True)
class _TerminalReadback:
    """Terminal-decision and durable-effect state read back from a session row."""

    terminal: bool
    terminal_status: Optional[str]
    effect_state: Optional[str]
    effect_pending: bool
    resolution: Dict[str, Any]
    hard_limit_blocked: bool


def _read_terminal_readback(
    persisted: Optional[Dict[str, Any]],
) -> _TerminalReadback:
    """Classify a re-read row (``load_session_as_dict``) for a caller that did
    not itself observe the terminal write: a late synthetic commit after a
    timeout, or another reviewer that finished while the fallback ran."""
    row = persisted or {}
    phase = str(row.get("phase") or "").lower()
    status = str(row.get("status") or "").lower()
    terminal_states = {"resolved", "failed"}
    terminal_status = (
        status if status in terminal_states
        else phase if phase in terminal_states
        else None
    )
    effect_state = row.get("resolution_effect_state")
    resolution = row.get("resolution")
    if not isinstance(resolution, dict):
        resolution = {}
    return _TerminalReadback(
        terminal=terminal_status is not None,
        terminal_status=terminal_status,
        effect_state=effect_state,
        effect_pending=(
            effect_state in {"pending", "applying", "unknown_partial"}
            or (terminal_status == "resolved" and effect_state != "applied")
        ),
        resolution=resolution,
        hard_limit_blocked=(
            terminal_status == "failed" and resolution.get("action") == "block"
        ),
    )


def _terminal_readback_next_step(
    readback: _TerminalReadback, session_id: str
) -> Optional[str]:
    """Guidance for a terminal readback, or None when the row is not terminal."""
    if readback.terminal_status == "resolved":
        return (
            "The terminal decision committed. An operator should "
            "inspect its durable effect with "
            "dialectic(action='reconcile_effect', "
            f"session_id='{session_id}')."
        )
    if readback.terminal_status == "failed":
        return (
            "The terminal failure committed. Review the durable "
            "failure reason before starting any replacement review."
        )
    return None


def _apply_terminal_readback(
    result: Dict[str, Any],
    readback: _TerminalReadback,
    *,
    default_failure_reason: str,
) -> None:
    """Copy terminal and effect state into a response without overstating it.

    A terminal row whose durable runtime effect is not confirmed applied is not
    a successful operation yet, and neither is a terminal failure.
    """
    result["terminal_committed"] = True if readback.terminal else None
    result["terminal_status"] = readback.terminal_status
    result["effect_pending"] = readback.effect_pending
    result["resolution_effect_state"] = readback.effect_state
    if readback.effect_pending:
        result["success"] = False
    if readback.terminal_status:
        result["phase"] = readback.terminal_status
    if readback.terminal_status == "failed":
        result["success"] = False
        result["action"] = readback.resolution.get("action", "failed")
        result["reason"] = readback.resolution.get(
            "reason", default_failure_reason
        )
        result["hard_limit_blocked"] = readback.hard_limit_blocked
        result["hard_limit_violation"] = (
            readback.resolution.get("reason")
            if readback.hard_limit_blocked
            else None
        )


def _thesis_session_advanced(
    session: DialecticSession, persisted: Optional[Dict[str, Any]]
) -> bool:
    """Whether the durable row moved past the post-thesis state held in memory.

    Any of a claimed reviewer slot, a different phase or a later transcript
    revision means another writer, usually a reviewer that claimed the slot,
    acted after the thesis was recorded.
    """
    if not persisted:
        return False
    persisted_phase = str(persisted.get("phase") or "").lower()
    return bool(
        persisted.get("reviewer") is not None
        or (persisted_phase and persisted_phase != session.phase.value)
        or int(persisted.get("transcript_revision") or 0)
        > int(getattr(session, "transcript_revision", 0))
    )


def _terminal_commit_refusal_response(
    session_id: str,
    commit: _ResolutionCommitResult,
    arguments: Dict[str, Any],
    *,
    changed_msg: str,
    refused_msg: str,
    recovery_action: str,
) -> Sequence[TextContent]:
    """Build standard governance refusal response when a terminal CAS commit is rejected."""
    in_progress = commit.origin == "beam_saga_in_flight"
    changed = commit.origin == "transcript_changed"
    return [error_response(
        (
            "Another terminal resolution is already being committed."
            if in_progress
            else changed_msg
            if changed
            else refused_msg
        ),
        error_code=(
            "SESSION_RESOLUTION_IN_PROGRESS"
            if in_progress
            else "DIALECTIC_TRANSCRIPT_CHANGED"
            if changed
            else "SESSION_RESOLUTION_WRITE_REFUSED"
        ),
        error_category="governance_refusal",
        details={
            "session_id": session_id,
            "commit_origin": commit.origin,
            "terminal_committed": False,
        },
        recovery={
            "action": recovery_action,
            "related_tools": ["dialectic"],
        },
        arguments=arguments,
    )]


def _resolution_effect_view(effect: Dict[str, Any]) -> Dict[str, Any]:
    """Stable, JSON-safe operator view of one resolution-effect record."""
    view = {
        "session_id": effect.get("session_id"),
        "state": effect.get("state"),
        "effect_required": effect.get("effect_required"),
        "pause_token": effect.get("pause_token"),
        "owner_token": effect.get("owner_token"),
        "attempt_count": int(effect.get("attempt_count") or 0),
        "last_error": effect.get("last_error"),
        "execution_result": effect.get("execution_result_json"),
    }
    for key in ("claimed_at", "applied_at", "uncertain_at", "updated_at"):
        value = effect.get(key)
        view[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return view


def _parse_resolution_effect_updated_at(value: Any) -> datetime:
    """Parse the opaque row version returned by resolution-effect inspect.

    The public surface transports PostgreSQL ``timestamptz`` values as ISO-8601
    strings.  Requiring an offset prevents a caller-local timezone assumption
    from silently selecting a different record version.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("effect_updated_at must be a non-empty ISO-8601 string")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
        )
    except ValueError as exc:
        raise ValueError(
            "effect_updated_at must be the ISO-8601 value from inspect"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("effect_updated_at must include a timezone offset")
    return parsed


def _resolution_effect_skips_resume_calibration(
    execution_result: Dict[str, Any],
) -> bool:
    """Return whether a no-op resume lacks an agreement signal to calibrate."""
    return bool(
        execution_result.get("superseded")
        or execution_result.get("reason") == "legacy_unbound_session"
    )


def _get_dialectic_next_steps(recommendation: str) -> List[str]:
    """Get next steps based on dialectic recommendation."""
    if recommendation == "RESUME":
        return default_resume_steps()
    elif recommendation == "COOLDOWN":
        return default_cooldown_steps()
    else:  # ESCALATE
        return default_escalate_steps()


