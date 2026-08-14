"""Governance telemetry feeds: EISV latest/recent/health, /api/events,
enforcement divergence, lifecycle recency, and the /ws/eisv stream.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
import time
from typing import Any

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

from src.http_routes import access

logger = get_logger(__name__)


# HTTP polling fallback for EISV (when WebSocket is blocked by proxy auth)
async def http_eisv_latest(request):
    """Return the latest EISV update as JSON (polling fallback for WebSocket)."""
    if broadcaster_instance.last_update:
        return JSONResponse(broadcaster_instance.last_update)
    return JSONResponse({"type": "no_data", "message": "No EISV updates yet"}, status_code=200)


async def http_eisv_recent(request):
    """Return the last N eisv_update events in chronological order.

    Backfill endpoint for dashboard clients that just connected — lets the
    chart populate immediately from the broadcaster's ring buffer instead of
    waiting for the next live check-in. Used both by WebSocket clients on
    reconnect and polling-fallback clients (when upstream proxies block the
    WS upgrade, e.g. Cloudflare tunnels without the WebSocket toggle).
    """
    try:
        limit = int(request.query_params.get("limit", 120))
    except (TypeError, ValueError):
        limit = 120
    limit = max(1, min(limit, 500))

    events: list = []
    for event in broadcaster_instance.event_history:
        if isinstance(event, dict) and event.get("type") == "eisv_update":
            events.append(event)
    events = events[-limit:]
    return JSONResponse({"type": "eisv_recent", "count": len(events), "events": events})


_EISV_TELEMETRY_HEALTH_CACHE_TTL_SECONDS = 30.0
_eisv_telemetry_health_cache: dict[int, tuple[float, dict[str, Any]]] = {}


async def http_eisv_telemetry_health(request):
    """GET /v1/eisv/telemetry-health?days=30 — fleet instrumentation health.

    Aggregates the append-only EISV telemetry envelope without feeding any
    result back into measurement, policy, or enforcement.  The outcome slice is
    strict-external and lead-separated; its calibration bins are descriptive,
    clustered audit evidence rather than a claim of predictive lift.

    The redesign refreshes monitor views every ten seconds, while this endpoint
    scans a durable multi-day cohort.  Cache each supported window briefly so a
    dashboard tab does not turn observability into database load.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        days = int(request.query_params.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 90))

    now = time.monotonic()
    cached = _eisv_telemetry_health_cache.get(days)
    if cached and now - cached[0] < _EISV_TELEMETRY_HEALTH_CACHE_TTL_SECONDS:
        return JSONResponse(cached[1], headers={"Cache-Control": "private, max-age=30"})

    try:
        from src.db import get_db
        from src.eisv_telemetry_health import query_eisv_telemetry_health

        db = get_db()
        async with db.acquire() as conn:
            report = await query_eisv_telemetry_health(conn, window_days=days)
        _eisv_telemetry_health_cache[days] = (now, report)
        return JSONResponse(report, headers={"Cache-Control": "private, max-age=30"})
    except Exception as exc:  # noqa: BLE001 — read-only operator surface
        logger.error("EISV telemetry health query failed: %s", exc)
        return JSONResponse(
            {"success": False, "error": "telemetry health query failed"},
            status_code=500,
        )


# Events API endpoint for dashboard
async def http_events(request):
    """Return recent governance events for dashboard."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        from src.event_detector import event_detector

        # A mistyped or unsupported filter key used to be dropped silently, so
        # the response came back 200 with the FULL unfiltered set — a caller
        # asking for one event type got everything and no indication the filter
        # had been ignored. Surfaced by the dogfood probe (finding 9028fa1e,
        # 2026-07-28) using `event_type`, which is what the MCP
        # observe(action='audit_events') surface calls the same parameter.
        # `event_type` is now an accepted alias; anything else is a 400 rather
        # than a silent full-table read.
        supported = {"limit", "agent_id", "type", "event_type", "since"}
        unknown = sorted(set(request.query_params.keys()) - supported)
        if unknown:
            return JSONResponse({
                "success": False,
                "error": f"Unsupported filter parameter(s): {', '.join(unknown)}",
                "next_step": "Remove the parameter or use a supported one.",
                "safe_options": sorted(supported),
            }, status_code=400)

        limit = int(request.query_params.get("limit", 50))
        agent_id = request.query_params.get("agent_id")
        # `type` wins when both are given, so existing callers are unaffected.
        event_type = request.query_params.get("type") or request.query_params.get("event_type")
        since_raw = request.query_params.get("since")
        since = int(since_raw) if since_raw is not None else None

        events = event_detector.get_recent_events(
            limit=limit,
            agent_id=agent_id,
            event_type=event_type,
            since=since
        )

        # Supplement from PostgreSQL when in-memory buffer is thin
        # (e.g. right after a restart)
        if len(events) < limit:
            try:
                from src.audit_db import query_audit_events_async
                from datetime import datetime, timedelta, timezone
                start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                db_events = await query_audit_events_async(
                    agent_id=agent_id,
                    event_type=event_type,
                    start_time=start_time,
                    limit=limit,
                    order="desc",
                )
                # Merge: use in-memory event_ids to deduplicate
                mem_ids = {e.get("event_id") for e in events if e.get("event_id")}
                # When `since` is given, audit rows with non-int event_ids (UUIDs)
                # are unreachable via the int-cursor protocol and would replay
                # every poll — drop them. See CIRWEL/unitares#25.
                int_cursor = since is not None
                for de in db_events:
                    de_id = de.get("event_id")
                    if de_id in mem_ids:
                        continue
                    if int_cursor:
                        try:
                            int(de_id)
                        except (TypeError, ValueError):
                            continue
                    # Reshape audit row → dashboard event shape
                    payload = de.get("details", {})
                    events.append({
                        "type": payload.get("type", de.get("event_type", "")),
                        "severity": payload.get("severity", "info"),
                        "message": payload.get("message", de.get("event_type", "")),
                        "agent_id": de.get("agent_id"),
                        "agent_name": payload.get("agent_name", ""),
                        "timestamp": de.get("timestamp"),
                        "event_id": de_id,
                    })
                events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
                events = events[:limit]
            except Exception as db_err:
                logger.debug(f"Audit DB supplement failed (non-fatal): {db_err}")

        return JSONResponse({
            "success": True,
            "events": events,
            "count": len(events)
        })
    except Exception as e:
        logger.error(f"Error fetching events: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "events": []
        }, status_code=500)


# Event types surfaced by /v1/lifecycle/recent. Pause-side answers
# "why did this agent stop?", resume-side answers "how did it come back?".
_LIFECYCLE_EVENT_TYPES = (
    "lifecycle_paused",
    "lifecycle_resumed",
    "lifecycle_archived",
    "lifecycle_loop_detected",
    "lifecycle_stuck_detected",
    "circuit_breaker_trip",
    "circuit_breaker_reset",
)


async def http_enforcement_divergence(request):
    """GET /v1/enforcement/divergence?days=90 — produced-vs-delivered honesty meter.

    A produced pause VERDICT is not a delivered enforcement ACTION: under the
    deployed advisory posture (ratified 2026-08-06, proprioception contract
    "Deployed posture"), gap-suppression downgrades pauses to proceed at any
    >150s inter-check-in gap. Operator surfaces that read verdict counts as
    enforcement counts are mislabeled against that posture. This endpoint
    reports both meters side by side — produced pauses (auto_attest
    decision='pause'), how many gap-suppressed, delivered pauses
    (lifecycle_paused), and the last delivered timestamp — so the divergence
    is visible by query, not memory. Read-only.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            days = int(request.query_params.get("days", "90"))
        except (TypeError, ValueError):
            days = 90
        days = max(1, min(days, 365))
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            totals = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (WHERE event_type = 'auto_attest'
                                     AND payload->>'decision' = 'pause') AS produced,
                  count(*) FILTER (WHERE event_type = 'auto_attest'
                                     AND payload->>'decision' = 'pause'
                                     AND payload->>'gap_suppressed' = 'true') AS gap_suppressed,
                  count(*) FILTER (WHERE event_type = 'lifecycle_paused') AS delivered
                FROM audit.events
                WHERE ts > now() - make_interval(days => $1)
                  AND event_type IN ('auto_attest', 'lifecycle_paused')
                """,
                days,
            )
            last_row = await conn.fetchrow(
                "SELECT max(ts) AS last_delivered FROM audit.events "
                "WHERE event_type = 'lifecycle_paused'"
            )
            weekly = await conn.fetch(
                """
                SELECT to_char(date_trunc('week', ts), 'MM-DD') AS week,
                       count(*) FILTER (WHERE event_type = 'auto_attest'
                                          AND payload->>'decision' = 'pause') AS produced,
                       count(*) FILTER (WHERE event_type = 'lifecycle_paused') AS delivered
                FROM audit.events
                WHERE ts > now() - make_interval(days => $1)
                  AND event_type IN ('auto_attest', 'lifecycle_paused')
                GROUP BY date_trunc('week', ts)
                ORDER BY date_trunc('week', ts)
                """,
                days,
            )
        last_delivered = last_row["last_delivered"] if last_row else None
        return JSONResponse({
            "window_days": days,
            "posture": "advisory",
            "produced_pauses": int(totals["produced"]),
            "gap_suppressed": int(totals["gap_suppressed"]),
            "delivered_pauses": int(totals["delivered"]),
            "last_delivered_at": last_delivered.isoformat() if last_delivered else None,
            "weekly": [
                {"week": r["week"], "produced": int(r["produced"]),
                 "delivered": int(r["delivered"])}
                for r in weekly
            ],
            "note": ("A produced pause verdict is not a delivered enforcement "
                     "action; delivered counts may include synthetic test "
                     "fixtures (see the proprioception contract)."),
        })
    except Exception as e:
        logger.error(f"enforcement divergence query failed: {e}")
        return JSONResponse({"error": "query failed"}, status_code=500)


async def http_lifecycle_recent(request):
    """GET /v1/lifecycle/recent — recent lifecycle / circuit-breaker events
    from audit.events with the full payload (reason, EISV, drift) and
    agent label resolution.

    Query params:
      - agent_id: filter to one agent (UUID or label)
      - hours: lookback window in hours (default 24, max 168)
      - limit: max events (default 100, max 500)
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        from src.audit_db import query_audit_events_async
        from datetime import datetime, timedelta, timezone

        agent_id_param = request.query_params.get("agent_id")
        hours = max(1, min(168, int(request.query_params.get("hours", 24))))
        limit = max(1, min(500, int(request.query_params.get("limit", 100))))
        start_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        # Resolve label → UUID and build a UUID → label lookup for enrichment.
        from src.agent_metadata_model import agent_metadata
        label_to_uuid = {}
        uuid_to_label = {}
        for uuid_, meta in agent_metadata.items():
            label = getattr(meta, "label", None) or ""
            if label:
                label_to_uuid[label] = uuid_
                uuid_to_label[uuid_] = label
        resolved_agent_id = label_to_uuid.get(agent_id_param, agent_id_param) \
            if agent_id_param else None

        rows = await query_audit_events_async(
            agent_id=resolved_agent_id,
            event_types=list(_LIFECYCLE_EVENT_TYPES),
            start_time=start_time,
            limit=limit,
            order="desc",
        )

        events = []
        for r in rows:
            details = r.get("details") or {}
            events.append({
                "timestamp": r.get("timestamp"),
                "event_type": r.get("event_type"),
                "agent_id": r.get("agent_id"),
                "agent_label": uuid_to_label.get(r.get("agent_id"), ""),
                "reason": details.get("reason"),
                "details": details,
                "event_id": r.get("event_id"),
            })

        return JSONResponse({
            "success": True,
            "events": events,
            "count": len(events),
            "window_hours": hours,
        })
    except Exception as e:
        logger.error(f"Error fetching lifecycle events: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "events": []
        }, status_code=500)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

async def websocket_eisv_stream(websocket):
    """WebSocket endpoint for live EISV streaming to dashboard."""
    if not access._check_ws_auth(websocket, http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN")):
        # Close before accept — uvicorn turns this into a 403 on the handshake,
        # so an unauthorized caller never reaches the broadcaster at all.
        await websocket.close(code=1008)
        return
    await broadcaster_instance.connect(websocket)
    try:
        while True:
            # Keep connection alive -- client sends pings, we just listen
            await websocket.receive_text()
    except Exception:
        await broadcaster_instance.disconnect(websocket)
