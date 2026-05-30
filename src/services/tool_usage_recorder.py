"""Records tool call telemetry to JSONL + audit.tool_usage.

Shared between STDIO transport (src/mcp_server_std.py) and HTTP transport
(src/services/http_tool_service.py). JSONL write is synchronous; DB write is
fire-and-forget via create_tracked_task so request handlers never await
asyncpg (anyio-asyncio deadlock rule).
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple

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
        error_type = category or payload.get("error_code") or "tool_error"
        return False, str(error_type)
    return True, None


def record_tool_usage(
    tool_name: str,
    agent_id: Optional[str],
    success: bool,
    error_type: Optional[str] = None,
    latency_ms: Optional[int] = None,
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
            ),
            name="persist_tool_usage",
        )
    except RuntimeError:
        pass  # no running event loop (CLI / tests)
    except Exception as e:
        logger.debug(f"DB tool_usage persist failed (non-fatal): {e}")
