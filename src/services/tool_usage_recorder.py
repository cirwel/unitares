"""Records tool call telemetry to JSONL + audit.tool_usage.

Shared between STDIO transport (src/mcp_server_std.py), HTTP transport
(src/services/http_tool_service.py) and the MCP-protocol tool wrapper
(src/tool_registration.py). JSONL write is synchronous; DB write is
fire-and-forget via create_tracked_task so request handlers never await
asyncpg (anyio-asyncio deadlock rule).

Payload discriminator (#1387). ``audit.tool_usage.tool_name`` alone cannot
answer "did an agent request a dialectic review?": ``dialectic`` is an
action_router, so ``dialectic(action='request')`` (a review request) and
``dialectic(action='list')`` (a dashboard read of the sessions table) are the
same row. ``build_tool_usage_payload`` writes a STRICTLY ALLOWLISTED,
three-key discriminator into the already-plumbed ``payload`` jsonb column.
The allowlist is a whitelist enforced in code, not a convention: no caller
value other than the action token ever reaches the payload, and that token is
clamped to the tool's own routing vocabulary.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from src.logging_utils import get_logger

logger = get_logger(__name__)


def _payload_from_result(result: Any) -> Optional[dict]:
    """Best-effort extraction of the JSON payload dict from a dispatched tool result.

    Handlers return either a single-element list of MCP TextContent (whose .text is
    a JSON string) or already-decoded data. Returns the decoded dict, or None when
    the result is not a recognizable JSON object (treated as success — no signal).
    """
    if isinstance(result, dict):
        return result
    # All known error-bearing responses put the payload in element 0; inspect it
    # regardless of list length so a multi-element response can't hide a failure.
    if isinstance(result, (list, tuple)) and result and hasattr(result[0], "text"):
        try:
            decoded = json.loads(result[0].text)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


# error_category values that are caused by governance (EISV) rather than by the
# caller or the substrate. AGENT_PAUSED / AGENT_ARCHIVED gate-refusals carry
# error_category="state_error"; counting them as tool failures would re-import the
# circularity this label exists to avoid (a paused agent's later calls fail ONLY
# because EISV paused it). Treat as no-signal to keep tool_usage.success EISV-blind.
_EISV_CAUSED_ERROR_CATEGORIES = frozenset({"state_error"})


# The #425 typed identity refusal is deliberately shaped as a SUCCESS response
# (``strict_identity_refusal_payload`` docstring: "A structured success-shape,
# not an error: error responses invite retry-with-mint catch paths and would
# reintroduce the ghost leak"). Every emission point wraps it in
# ``success_response``, which spreads it at the top level under
# ``success: True`` — so the plain ``success is False`` test below cannot see
# it, and a refused call would audit as a SUCCEEDING anonymous call.
#
# That is the exact poisoning the REST gate already works around by hand
# (``http_tool_service.execute_http_tool`` records success=False /
# error_type="identity_required" at its pre-dispatch refusal). Detecting the
# shape here extends that honesty to every OTHER refusal emission point —
# identity_step (MCP dispatch), consolidated.py, updates/phases.py Path-C,
# identity/handlers.py Path-B — none of which had a hand-written recorder.
#
# ``rollout_flag`` is set unconditionally by ``strict_identity_refusal_payload``
# and by nothing else in the codebase, so it is a precise single-sourced
# marker: no other success payload can false-positive on it.
_IDENTITY_REFUSAL_MARKER = "STRICT_IDENTITY_REQUIRED"


def _identity_refusal_status(payload: Any) -> Optional[str]:
    """Return the refusal ``status`` if this payload is a #425 typed refusal.

    ``status`` varies by emission point (``identity_required``,
    ``lineage_declaration_required``, ...) and is a bounded server-authored
    literal, so it is safe as an ``error_type``. Returns None for anything else.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("rollout_flag") != _IDENTITY_REFUSAL_MARKER:
        return None
    status = payload.get("status")
    return str(status) if status else "identity_required"


def classify_tool_result(result: Any) -> Tuple[bool, Optional[str]]:
    """Distinguish a genuine, EISV-blind tool failure from a successful call (possibly
    carrying a governance verdict) by inspecting the result payload.

    Only ``error_response()`` sets ``success: False`` (validation/auth/state/system
    errors). ``success_response()`` always sets ``success: True`` and spreads any
    governance ``verdict`` (pause/reject) into the payload — those are SUCCESSFUL
    tool calls and must NOT be flagged as failures. ``state_error`` refusals are
    excluded too: they are governance-caused (see ``_EISV_CAUSED_ERROR_CATEGORIES``),
    so counting them would make the label circular. The one success-SHAPED result
    that is not a success is the #425 typed identity refusal — see
    ``_identity_refusal_status``. Returns ``(success, error_type)``.
    """
    payload = _payload_from_result(result)
    refusal_status = _identity_refusal_status(payload)
    if refusal_status is not None:
        return False, refusal_status
    if isinstance(payload, dict) and payload.get("success") is False:
        category = payload.get("error_category")
        if category in _EISV_CAUSED_ERROR_CATEGORIES:
            return True, None  # governance-caused refusal — not an EISV-blind failure
        # Legacy error_response() refusals (e.g. the reserved-prefix guard in
        # validators.py) carry only a details-spread "error_type" — without
        # this fallback they audit as generic "tool_error". Six months of
        # reserved_prefix refusals (~820k rows, surfaced by #543) were
        # indistinguishable from real failures until a live repro.
        error_type = (
            category
            or payload.get("error_code")
            or payload.get("error_type")
            or "tool_error"
        )
        return False, str(error_type)
    return True, None


# Tools whose successful response MINTS (or freshly resolves) the caller's
# identity. Their audit rows can never be attributed from the request side —
# the UUID does not exist until the handler returns — so attribution falls
# back to the response payload. Kept to the minting family on purpose: a
# generic response-side fallback would silently re-attribute every
# auto-minted anonymous call and change the meaning of existing
# agent_id=NULL rows. Found 2026-06-12: onboard rows carried agent_id=NULL,
# making onboard→first-checkin conversion unmeasurable from audit.tool_usage.
_IDENTITY_MINTING_TOOLS = frozenset({"onboard", "start_session"})


# Off-path activity that proves process liveness without going through the
# ceremonial process_agent_update handler. The check-in path already refreshes
# presence directly; keep this list to value-bearing tools that otherwise leave
# onboard+work agents with an expiring agent:/ lease.
_PRESENCE_REFRESH_TOOLS = frozenset({
    "knowledge",
    "search_knowledge_graph",
    "store_knowledge_graph",
    "leave_note",
    "outcome_event",
    "observe",
    "observe_agent",
})


def _is_uuid_like(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _schedule_presence_refresh(
    *,
    tool_name: str,
    agent_id: Optional[str],
    success: bool,
    session_id: Optional[str],
) -> None:
    """Refresh agent:/ presence for successful off-path value activity."""
    if (
        not success
        or tool_name not in _PRESENCE_REFRESH_TOOLS
        or not _is_uuid_like(agent_id)
    ):
        return
    try:
        from src.mcp_handlers.identity.agent_presence_lease import (
            schedule_agent_presence_heartbeat,
        )

        schedule_agent_presence_heartbeat(str(agent_id), session_id)
    except Exception as e:  # pragma: no cover - observability must never break tools
        logger.debug(f"agent presence refresh scheduling failed (non-fatal): {e}")


def resolve_minted_agent_id(tool_name: str, agent_id: Optional[str], result: Any) -> Optional[str]:
    """Return the audit-attribution agent_id for a completed tool call.

    Request-side identity always wins. For identity-minting tools with no
    request-side identity, fall back to the UUID in the response payload —
    top-level ``uuid`` (canonical onboard), ``raw_governance.uuid`` (alias
    envelope, e.g. start_session), or ``agent_signature.uuid``. Returns the
    incoming ``agent_id`` unchanged in every other case; never raises.
    """
    if agent_id or tool_name not in _IDENTITY_MINTING_TOOLS:
        return agent_id
    payload = _payload_from_result(result)
    if not isinstance(payload, dict):
        return agent_id
    raw = payload.get("raw_governance")
    signature = payload.get("agent_signature")
    uuid = (
        payload.get("uuid")
        or (raw.get("uuid") if isinstance(raw, dict) else None)
        or (signature.get("uuid") if isinstance(signature, dict) else None)
    )
    return str(uuid) if uuid else agent_id


# ---------------------------------------------------------------------------
# Action discriminator payload (#1387)
# ---------------------------------------------------------------------------

# The COMPLETE set of keys that may appear in a tool_usage payload written by
# this module. A whitelist, not a blacklist: an argument key that is not
# produced by the builder below can never reach the payload, even if someone
# adds it to a Pydantic schema later. Enforced twice — the builder only ever
# emits these, and `_sanitize_payload` drops anything else before the write.
_ALLOWED_PAYLOAD_KEYS = frozenset({"action", "canonical_tool", "action_source"})

# Written instead of the caller's action string when the action does not
# belong to the tool's own routing vocabulary (external plugin router, typo,
# an action added without updating the router map). Closes the cardinality
# hole where `dialectic(action="<4KB of junk>")` becomes a GROUP BY key.
_ACTION_UNLISTED = "action_unlisted"

# Defense in depth. Every value the builder emits is already a bounded
# server-side literal (a routing-table key, a tool-registry name, or one of
# three source constants), so this cap can only ever fire if the clamp is
# bypassed by a future edit. 64 is comfortably above the longest real value.
_MAX_PAYLOAD_VALUE_LEN = 64


def _sanitize_payload(payload: Any) -> Optional[Dict[str, str]]:
    """Drop anything not on the allowlist; coerce to flat str->str. Never raises.

    Applied at the write boundary so that even a caller passing a hand-built
    dict (tests, a future call site) cannot land a credential, a free-text
    field, or a nested structure in ``audit.tool_usage.payload``.
    """
    if not isinstance(payload, dict) or not payload:
        return None
    try:
        clean: Dict[str, str] = {}
        for key, value in payload.items():
            if key not in _ALLOWED_PAYLOAD_KEYS:
                continue
            if not isinstance(value, str) or not value:
                continue
            if len(value) > _MAX_PAYLOAD_VALUE_LEN:
                continue
            clean[key] = value
        return clean or None
    except Exception as e:  # pragma: no cover - telemetry must never break a tool
        logger.debug(f"tool_usage payload sanitize failed (non-fatal): {e}")
        return None


def build_tool_usage_payload(
    tool_name: str, arguments: Any
) -> Optional[Dict[str, str]]:
    """Build the bounded action discriminator for one tool call. Never raises.

    MUST be called BEFORE dispatch. ``run_tool_dispatch_pipeline`` does not
    copy ``arguments`` and ``params_step.resolve_alias`` mutates it in place
    (``arguments["action"] = alias_info.inject_action``), so a post-dispatch
    build would see an alias-injected action as caller-explicit and every
    ``request_review`` row would claim ``action_source="explicit"``.

    Shape (max three keys, all ``str``, no nesting):

      ``action``         the resolved sub-action, clamped to the tool's own
                         ``known_actions`` routing map, or ``action_unlisted``.
                         Absent for single-purpose tools — ``payload`` stays
                         ``'{}'`` for the ~97% of rows (get_governance_metrics,
                         list_agents, process_agent_update, ...) where a
                         sub-action is not a coherent question.
      ``canonical_tool`` only when the invoked name differs from the canonical
                         one, so ``COALESCE(payload->>'canonical_tool',
                         tool_name)`` recovers the canonical view while
                         ``tool_name`` keeps recording what the caller typed.
      ``action_source``  explicit | alias_injected | default. Load-bearing:
                         ``dialectic`` defaults to ``list``, so without this a
                         router default is indistinguishable from an agent
                         explicitly asking.

    Deliberately absent: every caller-authored value. No ``continuity_token``
    (a signed HMAC ownership proof, and a legal parameter on nearly every
    tool), no free text (``issue_description``, ``reasoning``, ``reflection``,
    ``query``, ...), no structures, no paths, and no identifiers that are
    already columns (``agent_id``, ``client_session_id``).
    """
    try:
        from src.mcp_handlers.decorators import (
            get_tool_definition,
            resolve_canonical_action_and_source,
        )

        canonical, action, source = resolve_canonical_action_and_source(
            tool_name, arguments
        )
        payload: Dict[str, str] = {}
        if canonical and canonical != tool_name:
            payload["canonical_tool"] = str(canonical)

        td = get_tool_definition(canonical) if canonical else None
        known = getattr(td, "known_actions", None) if td is not None else None

        if action:
            if known is not None:
                payload["action"] = action if action in known else _ACTION_UNLISTED
            elif td is None:
                # Unregistered name (external plugin router, e.g. `pi` from
                # unitares_pi_plugin). We cannot know its vocabulary, so we
                # record that a sub-action existed WITHOUT recording the
                # caller's string.
                payload["action"] = _ACTION_UNLISTED
            # td is registered but declares no vocabulary => single-purpose
            # tool that happened to receive a stray `action` kwarg. Record
            # nothing: "absence means no sub-action" stays true.
            if "action" in payload and source:
                payload["action_source"] = source

        return _sanitize_payload(payload)
    except Exception as e:  # pragma: no cover - telemetry must never break a tool
        logger.debug(f"tool_usage payload build failed (non-fatal): {e}")
        return None


def resolve_audit_agent_id(agent_id: Optional[str]) -> Optional[str]:
    """Fall back to the RESOLVED session binding when the request carried no agent_id.

    ``record_tool_usage``'s ``agent_id`` has always been purely request-side —
    ``arguments.get("agent_id")``. Identity actually lives in the contextvar the
    identity middleware sets (``update_context_agent_id``), so every call whose
    binding was resolved rather than passed audited as anonymous: `identity` is
    in ``_resolve_http_bound_agent``'s ``skip_tools`` and returns before
    injecting, and every ``pre_onboard`` read hits the #945 guard and returns
    ``None`` — 3,049 `identity` rows and 52,712 `list_agents` rows in 30 days,
    all with ``agent_id IS NULL``.

    Request-side ALWAYS wins (unchanged). The fallback is clamped to UUID-shaped
    values on purpose: ``set_session_context`` seeds the contextvar from the
    unverified ``X-Agent-Id`` header before any resolution runs, and
    ``audit.tool_usage.agent_id`` already carries a mix of UUIDs and structured
    labels. Admitting only UUIDs means the fallback can add a joinable key and
    nothing else. Never raises.
    """
    if agent_id:
        return agent_id
    try:
        from src.mcp_handlers.context import get_context_agent_id

        bound = get_context_agent_id()
    except Exception:  # pragma: no cover - observability must never break tools
        return agent_id
    return bound if _is_uuid_like(bound) else agent_id


def record_tool_usage(
    tool_name: str,
    agent_id: Optional[str],
    success: bool,
    error_type: Optional[str] = None,
    latency_ms: Optional[int] = None,
    session_id: Optional[str] = None,
    payload: Optional[Dict[str, str]] = None,
) -> None:
    """Record a tool call. Never raises — telemetry failure must not break the call."""
    # Presence refresh deliberately keys on the REQUEST-side agent_id, not the
    # context fallback: the agent:/ presence lease is a liveness claim on a
    # different surface, and widening who refreshes it is not a telemetry
    # change. Audit attribution is resolved below, for the audit writes only.
    request_side_agent_id = agent_id
    agent_id = resolve_audit_agent_id(agent_id)
    payload = _sanitize_payload(payload)
    try:
        from src.tool_usage_tracker import get_tool_usage_tracker
        get_tool_usage_tracker().log_tool_call(
            tool_name=tool_name, agent_id=agent_id, success=success, error_type=error_type,
        )
    except Exception as e:
        logger.debug(f"JSONL tool_usage log failed (non-fatal): {e}")

    try:
        from src.background_tasks import create_tracked_task
        from src.audit_db import append_tool_usage_async
        create_tracked_task(
            append_tool_usage_async(
                agent_id=agent_id,
                tool_name=tool_name,
                latency_ms=latency_ms,
                success=success,
                error_type=error_type,
                session_id=session_id,
                payload=payload,
            ),
            name="persist_tool_usage",
        )
    except RuntimeError:
        pass  # no running event loop (CLI / tests)
    except Exception as e:
        logger.debug(f"DB tool_usage persist failed (non-fatal): {e}")

    _schedule_presence_refresh(
        tool_name=tool_name,
        agent_id=request_side_agent_id,
        success=success,
        session_id=session_id,
    )
