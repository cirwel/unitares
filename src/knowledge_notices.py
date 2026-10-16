"""Bounded KG change pointers; reads never consume, explicit receipts acknowledge.

The canonical-table trigger coalesces intermediate revisions into the latest
pointer. No global sequence cursor can skip a late-committing transaction.
A receipt identifies one recipient/row/revision and cannot acknowledge a newer
revision. One UUID is one process inbox; successor identities inherit nothing.
"""
from __future__ import annotations

import json
from uuid import UUID

from mcp.types import TextContent

NOTICE_LIMIT = 3
NOTICE_BYTES = 2048


def proven_recipient() -> str | None:
    from src.mcp_handlers.context import get_context_agent_id, get_session_proof_origin

    if get_session_proof_origin() != "caller_asserted":
        return None
    return get_context_agent_id()


def _receipts(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > NOTICE_LIMIT:
        raise ValueError("acknowledge must contain at most three revision UUIDs")
    return [str(UUID(str(item))) for item in value]


async def read_notices(recipient: str, *, acknowledge=None, acknowledge_gap=None, muted=None, db=None):
    """Read current pointers, optionally acknowledge exact revisions or mute."""
    receipts = _receipts(acknowledge)
    gap_receipt = str(UUID(str(acknowledge_gap))) if acknowledge_gap is not None else None
    if muted is not None and not isinstance(muted, bool):
        raise ValueError("muted must be a boolean")
    if db is None:
        from src.db import get_db
        db = get_db()
    prefs, rows = await db.kg_read_change_notices(
        recipient, [UUID(r) for r in receipts],
        UUID(gap_receipt) if gap_receipt else None, muted,
    )
    result = {"notices": [], "muted": prefs["muted"], "has_more": len(rows) > NOTICE_LIMIT,
              "ack_action": "knowledge(action='notices', acknowledge=[revision UUIDs])"}
    if prefs["gap_token"]:
        result.update(gap=True, gap_token=str(prefs["gap_token"]),
                      gap_hint="Delivery backlog expired or exceeded 100 rows. Re-scan relevant rows before acknowledging this gap.")
    for row in rows[:NOTICE_LIMIT]:
        item = {"discovery_id": row["discovery_id"], "revision": str(row["revision"]),
                "kind": row["change_kind"], "changed_fields": list(row["changed_fields"]),
                "details": {"action": "details", "discovery_id": row["discovery_id"]}}
        candidate = {**result, "notices": [*result["notices"], item]}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > NOTICE_BYTES:
            result["has_more"] = True
            break
        result["notices"].append(item)
    return result


async def handle_knowledge_notices(arguments):
    from src.mcp_handlers.utils import error_response, success_response

    recipient = proven_recipient()
    if not recipient:
        return [error_response("Knowledge notices require a caller-proven session; pass your client_session_id.")]
    try:
        data = await read_notices(recipient, acknowledge=arguments.get("acknowledge"),
                                  acknowledge_gap=arguments.get("acknowledge_gap"), muted=arguments.get("muted"))
    except ValueError as exc:
        return [error_response(str(exc))]
    data["next_action"] = "Read current details before acting; acknowledge only revisions you received with knowledge(action='notices', acknowledge=[...])."
    return success_response(data)


async def attach_notices(name, arguments, ctx, result):
    """Active-call delivery only, with no work spawned and no model invocation."""
    from src.mcp_handlers.identity_bootstrap import identity_refusal_status
    from src.logging_utils import get_logger

    recipient = proven_recipient()
    if not recipient or (name == "knowledge" and arguments.get("action") == "notices"):
        return result
    if not (isinstance(result, (list, tuple)) and result and hasattr(result[0], "text")):
        return result
    try:
        payload = json.loads(result[0].text)
        if not isinstance(payload, dict) or payload.get("success") is False or "error" in payload or payload.get("refused") or identity_refusal_status(payload):
            return result
        if "knowledge_changes" in payload or ("notices" in payload and "ack_action" in payload):
            return result  # use_tool's outer dispatch must not attach a second copy
        data = await read_notices(recipient)
        if data["muted"] or not (data["notices"] or data.get("gap")):
            return result
        payload["knowledge_changes"] = data
        # Preserve the envelope's measured-without-self receipt after adding
        # the separately budgeted notice field.
        size = payload.get("_response_size")
        if isinstance(size, dict):
            measured = len(json.dumps({k: v for k, v in payload.items() if k != "_response_size"}, ensure_ascii=False).encode())
            size.update(approx_bytes=measured, approx_kb=round(measured / 1000, 1),
                        size_class="small" if measured < 4000 else "medium" if measured < 12000 else "large")
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)), *result[1:]]
    except Exception:
        # Failed delivery never rolls back a successful tool. The durable pointer
        # remains unread/available on the next call, including after a restart.
        get_logger(__name__).warning("KG notice delivery unavailable; backlog retained", exc_info=True)
        return result
