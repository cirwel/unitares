"""
Audit Log Storage

PostgreSQL-backed audit event access via get_db().

The canonical raw truth is `data/audit_log.jsonl` (append-only).
PostgreSQL audit.events table provides indexed querying.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ``audit.tool_usage`` is one table holding two incompatible eras, and nothing
# about its shape says so.
#
# Until PR #1424 deployed, the FastMCP wrapper serving ``/mcp`` and ``/sse`` —
# i.e. every MCP-protocol client, including Claude Code — recorded nothing. Only
# REST callers landed rows. So any count, adoption figure, or per-agent
# attribution computed over a window reaching before this instant measured hooks
# and pollers, not agents, and a trend line spanning it measures the
# instrumentation change rather than any behaviour.
#
# The value is empirical, not a guess from the merge date: it is the first row
# carrying the ``action_source`` payload key that #1424 introduced
# (``select min(ts) from audit.tool_usage where payload ? 'action_source'``),
# which is also where distinct agents/day steps up. Recorded in UTC because the
# server stores UTC and a Denver-local bound here would reintroduce the
# partition off-by-six-hours class of bug.
TOOL_USAGE_MCP_INSTRUMENTED_SINCE = datetime(2026, 7, 31, 19, 24, 49, tzinfo=timezone.utc)


def tool_usage_window_coverage(
    window_hours: float,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Describe what a lookback window can and cannot support.

    Returns ``None`` when the window lies entirely inside the instrumented era —
    the common case, and one that should not carry a caveat it does not need.
    Otherwise returns a block naming the boundary and what is missing before it,
    so a caller reporting the number has to see the limit at the same time.
    """
    current = now or datetime.now(timezone.utc)
    start = current - timedelta(hours=float(window_hours))
    if start >= TOOL_USAGE_MCP_INSTRUMENTED_SINCE:
        return None

    return {
        "partial": True,
        "window_start": start.isoformat(),
        "mcp_instrumented_since": TOOL_USAGE_MCP_INSTRUMENTED_SINCE.isoformat(),
        "caveat": (
            "This window predates MCP-transport instrumentation. Rows before "
            "mcp_instrumented_since capture REST callers only (hooks, pollers, "
            "dashboard) — agent tool use over /mcp and /sse was not recorded. "
            "Counts spanning the boundary measure the instrumentation change, "
            "not behaviour. Narrow the window to the instrumented era before "
            "citing adoption, per-agent attribution, or a trend."
        ),
    }


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


async def aggregate_audit_events_async(
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    event_types: Optional[List[str]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Window-wide audit aggregates, independent of any row limit.

    Companion to query_audit_events_async: that one returns a bounded page of
    events, this one describes the whole matching set. Summaries built from the
    page misreport totals and timestamps whenever the page is smaller than the
    window.

    Returns one dict per (agent_id, event_type) with `count`, `first_ts` and
    `last_ts`. Timestamps are datetimes, not strings -- combine them with
    min()/max() rather than by string comparison, which is wrong across
    differing UTC offsets.
    """
    from src.db import get_db
    db = get_db()
    if not hasattr(db, '_pool') or db._pool is None:
        await db.init()

    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    return await db.aggregate_audit_events(
        agent_id=agent_id,
        event_type=event_type,
        event_types=event_types,
        start_time=start_dt,
        end_time=end_dt,
    )


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

    ``lease.*`` rows are NOT tool calls. They are lease-plane events projected
    into this table by the BEAM outbox forwarder
    (``elixir/lease_plane/lib/unitares_lease_plane/audit_outbox_forwarder.ex``),
    and ~99.9% of them are ``holder_class=process_instance`` presence
    heartbeats from ordinary session onboarding — substrate emission, not
    agent action. Counted undifferentiated, that heartbeat volume read as top
    tool/coordination throughput (4 of the top-10 "most used tools" at one 7d
    reading). The unfiltered aggregate therefore excludes ``lease.%`` rows
    from ``tools``/``most_used``/``total_calls`` and reports them separately
    under ``lease_plane``, split by ``payload->>'holder_class'`` so the volume
    stays visible but labeled as substrate. Excluding them also converges the
    DB and JSONL sources — the JSONL sink never receives ``lease.*`` rows. An
    explicit ``tool_name='lease.acquire'`` (or any ``lease.*``) query still
    answers with the raw rows.
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
        else:
            # Forwarded lease-plane substrate rows are excluded from the
            # aggregate and reported under ``lease_plane`` below; a scoped
            # query for an exact lease.* tool_name still answers directly.
            where.append("tool_name NOT LIKE 'lease.%'")
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
            lease_rows = None
            if not tool_name:
                lease_where = [
                    "ts > now() - ($1 * interval '1 hour')",
                    "tool_name LIKE 'lease.%'",
                ]
                lease_params: List[Any] = [float(window_hours)]
                if agent_id:
                    lease_params.append(agent_id)
                    lease_where.append(f"agent_id = ${len(lease_params)}")
                lease_sql = (
                    "SELECT tool_name, "
                    "coalesce(payload->>'holder_class', 'unknown') AS holder_class, "
                    "count(*)::bigint AS total_calls "
                    "FROM audit.tool_usage "
                    "WHERE " + " AND ".join(lease_where) + " "
                    "GROUP BY tool_name, holder_class"
                )
                lease_rows = await conn.fetch(lease_sql, *lease_params)
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

    lease_plane: Optional[Dict[str, Any]] = None
    if lease_rows is not None:
        lease_events: Dict[str, Dict[str, int]] = {}
        lease_total = 0
        for r in sorted(lease_rows, key=lambda r: int(r["total_calls"]), reverse=True):
            n = int(r["total_calls"])
            lease_events.setdefault(r["tool_name"], {})[r["holder_class"]] = n
            lease_total += n
        lease_plane = {
            "total_events": lease_total,
            "events": lease_events,
            "note": (
                "Lease-plane events forwarded into audit.tool_usage by the "
                "BEAM outbox forwarder — substrate emission, not tool calls. "
                "holder_class=process_instance rows are presence heartbeats "
                "from ordinary session onboarding; substrate_earned rows are "
                "the actual coordination trace. Excluded from "
                "tools/most_used/total_calls above."
            ),
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
        # Present only on the unfiltered path (no tool_name scope): the
        # forwarded lease-plane substrate volume, split by holder_class.
        **({"lease_plane": lease_plane} if lease_plane is not None else {}),
        # Present only when the window reaches into the pre-instrumentation era.
        # Omitted entirely otherwise, so its presence is the signal.
        **(
            {"coverage": coverage}
            if (coverage := tool_usage_window_coverage(window_hours))
            else {}
        ),
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
