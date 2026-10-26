"""Opt-in session views for callers checking review progress."""

from typing import Any


PREVIEW_CHARS = 280


def compact_session_read(payload: dict[str, Any]) -> dict[str, Any]:
    """Omit reconstruction bodies while retaining verdicts and control flow.

    Work on copies: PostgreSQL and the in-process cache may reuse the full
    dictionaries. Resolution conditions, attestation and actionability are
    deliberately left intact; compact is not a byte limit on those records.
    """
    result = dict(payload)
    if result.get("session_id"):
        omitted = []
        # The session-id/cache read uses transcript; raw PostgreSQL rows on
        # the agent-id read use messages. Both contain the full message bodies.
        for field in ("transcript", "messages", "paused_agent_state"):
            if field in result:
                value = result.pop(field)
                omitted.append(field)
                if field in ("transcript", "messages") and isinstance(value, list):
                    result.setdefault("message_count", len(value))
        for field in ("topic", "reason"):
            value = result.get(field)
            if isinstance(value, str) and len(value) > PREVIEW_CHARS:
                result[field] = value[:PREVIEW_CHARS].rstrip() + "…"
                result[f"{field}_truncated"] = True
                result[f"{field}_chars"] = len(value)
        result["fields"] = "compact"
        result["omitted_fields"] = omitted
        result["full_session_retrieval"] = {
            "tool": "dialectic",
            "arguments": {
                "action": "get",
                "session_id": result["session_id"],
                "fields": "full",
                "check_timeout": False,
            },
        }
    # Timeout/facilitation responses wrap the post-transition session in an
    # error envelope. Keep the error and its recovery text, project its body.
    if isinstance(result.get("session"), dict):
        result["session"] = compact_session_read(result["session"])
    if isinstance(result.get("sessions"), list):
        result["sessions"] = [compact_session_read(row) for row in result["sessions"]]
    return result
