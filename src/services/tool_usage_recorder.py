"""Records tool call telemetry to JSONL + audit.tool_usage.

Shared between STDIO transport (src/mcp_server_std.py) and HTTP transport
(src/services/http_tool_service.py). JSONL write is synchronous; DB write is
fire-and-forget via create_tracked_task so request handlers never await
asyncpg (anyio-asyncio deadlock rule).
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple
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


def classify_tool_result(result: Any) -> Tuple[bool, Optional[str]]:
    """Distinguish a genuine, EISV-blind tool failure from a successful call (possibly
    carrying a governance verdict) by inspecting the result payload.

    Only ``error_response()`` sets ``success: False`` (validation/auth/state/system
    errors). ``success_response()`` always sets ``success: True`` and spreads any
    governance ``verdict`` (pause/reject) into the payload — those are SUCCESSFUL
    tool calls and must NOT be flagged as failures. ``state_error`` refusals are
    excluded too: they are governance-caused (see ``_EISV_CAUSED_ERROR_CATEGORIES``),
    so counting them would make the label circular. Returns ``(success, error_type)``.
    """
    payload = _payload_from_result(result)
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


def record_tool_usage(
    tool_name: str,
    agent_id: Optional[str],
    success: bool,
    error_type: Optional[str] = None,
    latency_ms: Optional[int] = None,
    session_id: Optional[str] = None,
) -> None:
    """Record a tool call. Never raises — telemetry failure must not break the call."""
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
            ),
            name="persist_tool_usage",
        )
    except RuntimeError:
        pass  # no running event loop (CLI / tests)
    except Exception as e:
        logger.debug(f"DB tool_usage persist failed (non-fatal): {e}")

    _schedule_presence_refresh(
        tool_name=tool_name,
        agent_id=agent_id,
        success=success,
        session_id=session_id,
    )
