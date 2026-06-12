"""
Audit Log Storage

PostgreSQL-backed audit event access via get_db().

The canonical raw truth is `data/audit_log.jsonl` (append-only).
PostgreSQL audit.events table provides indexed querying.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


async def append_audit_event_async(entry: Dict[str, Any], raw_hash: Optional[str] = None) -> bool:
    """Append an audit event to PostgreSQL."""
    from src.db import get_db
    from src.db.base import AuditEvent
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()

    event = AuditEvent(
        ts=datetime.fromisoformat(entry["timestamp"]) if isinstance(entry.get("timestamp"), str) else entry.get("timestamp") or datetime.now(timezone.utc),
        event_id=entry.get("event_id", ""),
        event_type=entry.get("event_type", ""),
        agent_id=entry.get("agent_id"),
        session_id=entry.get("session_id"),
        confidence=float(entry.get("confidence", 1.0)),
        payload=entry.get("details", {}),
        raw_hash=raw_hash,
    )
    return await db.append_audit_event(event)


async def query_audit_events_async(
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    event_types: Optional[List[str]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 1000,
    order: str = "asc",
) -> List[Dict[str, Any]]:
    """Query audit events from PostgreSQL. Pass event_types for IN-list filtering."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()

    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    events = await db.query_audit_events(
        agent_id=agent_id,
        event_type=event_type,
        event_types=event_types,
        start_time=start_dt,
        end_time=end_dt,
        limit=limit,
        order=order,
    )
    return [
        {
            "timestamp": e.ts.isoformat() if e.ts else None,
            "agent_id": e.agent_id,
            "event_type": e.event_type,
            "confidence": e.confidence,
            "details": e.payload,
            "event_id": e.event_id,
        }
        for e in events
    ]


async def append_tool_usage_async(
    agent_id: Optional[str],
    tool_name: str,
    latency_ms: Optional[int],
    success: bool,
    error_type: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Append a tool_usage event to PostgreSQL. Returns False on failure — never raises."""
    try:
        from src.db import get_db
        db = get_db()
        if not hasattr(db, '_pool') or db._pool is None:
            await db.init()
        return await db.append_tool_usage(
            agent_id=agent_id,
            session_id=session_id,
            tool_name=tool_name,
            latency_ms=latency_ms,
            success=success,
            error_type=error_type,
            payload=payload,
        )
    except Exception:
        return False


async def audit_health_check_async() -> Dict[str, Any]:
    """Health check for audit storage backend."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()
    health = await db.health_check()
    health["component"] = "audit"
    return health
