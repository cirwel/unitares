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


async def get_tool_usage_stats_async(
    window_hours: float = 24 * 7,
    tool_name: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read tool-usage stats from the live ``audit.tool_usage`` DB sink.

    Returns the same shape as ``ToolUsageTracker.get_usage_stats`` (a drop-in for
    the legacy JSONL reader), or ``None`` when the DB is unavailable so callers can
    fall back to the JSONL tracker. Never raises.

    ``audit.tool_usage`` is the authoritative sink — written on every dispatched
    call by ``append_tool_usage_async``. The JSONL sink is best-effort and has
    drifted stale, so the readers prefer this path.
    """
    try:
        from src.db import get_db
        db = get_db()
        if not hasattr(db, "_pool") or db._pool is None:
            await db.init()

        where = ["ts > now() - ($1 * interval '1 hour')"]
        params: List[Any] = [float(window_hours)]
        if tool_name:
            params.append(tool_name)
            where.append(f"tool_name = ${len(params)}")
        if agent_id:
            params.append(agent_id)
            where.append(f"agent_id = ${len(params)}")
        sql = (
            "SELECT tool_name, "
            "count(*)::bigint AS total_calls, "
            "count(*) FILTER (WHERE success)::bigint AS success_count "
            "FROM audit.tool_usage "
            "WHERE " + " AND ".join(where) + " "
            "GROUP BY tool_name"
        )
        async with db.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception:
        return None

    from src.tool_usage_tracker import ToolUsageTracker
    removed = ToolUsageTracker.REMOVED_TOOLS

    counts = []
    for r in rows:
        t = r["tool_name"]
        if not t or t in removed:
            continue
        total = int(r["total_calls"])
        ok = int(r["success_count"])
        counts.append((t, total, ok))

    counts.sort(key=lambda x: x[1], reverse=True)
    total_calls = sum(c[1] for c in counts)

    tool_stats: Dict[str, Any] = {}
    for t, total, ok in counts:
        tool_stats[t] = {
            "total_calls": total,
            "success_count": ok,
            "error_count": total - ok,
            "success_rate": (ok / total) if total else 0.0,
            "percentage_of_total": (total / total_calls * 100) if total_calls else 0.0,
        }

    sorted_tools = [(t, total) for (t, total, _ok) in counts]
    return {
        "total_calls": total_calls,
        "unique_tools": len(tool_stats),
        "window_hours": window_hours,
        "tools": tool_stats,
        "most_used": [{"tool": t, "calls": c} for t, c in sorted_tools[:10]],
        "least_used": [{"tool": t, "calls": c} for t, c in sorted_tools[-10:]],
        "agent_usage": (
            {agent_id: {t: s["total_calls"] for t, s in tool_stats.items()}}
            if agent_id else None
        ),
        "source": "db",
    }


async def audit_health_check_async() -> Dict[str, Any]:
    """Health check for audit storage backend."""
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()
    health = await db.health_check()
    health["component"] = "audit"
    return health
