"""Service health and introspection: /health*, Prometheus /metrics, /debug/memory.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from starlette.responses import JSONResponse, Response

from prometheus_client import REGISTRY, generate_latest, CONTENT_TYPE_LATEST

from src.logging_utils import get_logger
from src.metrics_registry import (
    AGENTS_TOTAL,
    DIALECTIC_SESSIONS_ACTIVE,
    SERVER_INFO,
    SERVER_UPTIME,
)

from src.http_routes import access

logger = get_logger(__name__)


async def http_health(request):
    """Health check endpoint -- always public (monitoring, load balancers)"""

    # These are injected by register_http_routes via request.state
    server_ready = request.state._http_api_server_ready_fn()
    server_start_time = request.state._http_api_server_start_time
    server_version = request.state._http_api_server_version
    server_build_sha = getattr(request.state, "_http_api_server_build_sha", "unknown")
    has_streamable_http = request.state._http_api_has_streamable_http
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")

    # Calculate uptime
    uptime_seconds = time.time() - server_start_time
    uptime_hours = uptime_seconds / 3600
    uptime_days = uptime_hours / 24

    # Format uptime string
    if uptime_days >= 1:
        uptime_str = f"{int(uptime_days)}d {int((uptime_hours % 24))}h {int((uptime_seconds % 3600) / 60)}m"
    elif uptime_hours >= 1:
        uptime_str = f"{int(uptime_hours)}h {int((uptime_seconds % 3600) / 60)}m {int(uptime_seconds % 60)}s"
    else:
        uptime_str = f"{int(uptime_seconds / 60)}m {int(uptime_seconds % 60)}s"

    # DB pool health
    db_health = {"status": "unknown"}
    try:
        from src.db import get_db
        db = get_db()
        if hasattr(db, '_pool') and db._pool is not None:
            pool = db._pool
            db_health = {
                "status": "connected",
                "pool_size": pool.get_size(),
                "pool_idle": pool.get_idle_size(),
                "pool_max": pool.get_max_size(),
            }
        else:
            db_health = {"status": "no_pool"}
    except Exception as e:
        # /health is public (no auth) — log the detail server-side, don't leak
        # the raw exception text (DSN, host, internal paths) in the response.
        logger.warning("/health DB pool check failed: %s", e, exc_info=True)
        db_health = {"status": "error"}

    return JSONResponse({
        "status": "ok" if server_ready else "warming_up",
        "version": server_version,
        "build_sha": server_build_sha,
        "uptime": {
            "seconds": int(uptime_seconds),
            "formatted": uptime_str,
            "started_at": datetime.fromtimestamp(server_start_time).isoformat() if server_start_time else None
        },
        "database": db_health,
        "transports": {
            "streamable_http": "/mcp (primary, JSON response mode)" if has_streamable_http else "not available",
        },
        "endpoints": {
            "list_tools": "GET /v1/tools",
            "call_tool": "POST /v1/tools/call",
            "health": "GET /health",
            "metrics": "GET /metrics",
            "dashboard": "GET /dashboard"
        },
        "auth": {
            "enabled": bool(http_api_token),
            "header": "Authorization: Bearer <token>" if http_api_token else None
        },
        "session": {
            "header": "X-Session-ID (recommended for stable identity binding)"
        },
        "identity": {
            "header": "X-Agent-Id",
            "description": (
                "Compatibility hint only; never trusted as a REST identity binding. "
                "Use X-Session-ID or the client_session_id returned by onboard."
            )
        },
        "note": "Use /mcp for MCP clients (Streamable HTTP)."
    })


async def http_health_live(request):
    """Liveness probe — server process is up. Always public, no checks."""
    return JSONResponse({"status": "alive"})


async def http_health_ready(request):
    """Readiness probe — server has completed warmup and is accepting requests."""
    server_ready = request.state._http_api_server_ready_fn()
    if server_ready:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "warming_up"}, status_code=503)


async def http_health_deep(request):
    """Deep health — reads the cached snapshot produced by deep_health_probe_task.

    Does NOT touch the DB at request time (see
 ). If the probe has not populated
    the cache yet, returns 503 and instructs the caller to retry.
    """
    # Gated, unlike /health, /health/live and /health/ready: the deep snapshot
    # is the FULL diagnostic view (identity and active-session counts, Redis
    # key cardinality by category, Pi connectivity URLs, and raw exception
    # strings the shallow probe deliberately withholds), so it is operator
    # detail rather than a liveness signal. Monitoring keeps using /health.
    # Both dashboard callers already authenticate (data.js rest() -> authFetch).
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    from src.services.health_snapshot import (
        get_snapshot,
        is_stale,
        PROBE_INTERVAL_SECONDS,
        STALENESS_THRESHOLD_SECONDS,
    )

    snapshot, age_seconds, produced_at = get_snapshot()
    if snapshot is None:
        return JSONResponse(
            {
                "status": "unavailable",
                "error": "Health snapshot not yet populated — deep probe has not run.",
                "retry_after_seconds": 5,
            },
            status_code=503,
        )

    response = dict(snapshot)
    response["_cache"] = {
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "produced_at": produced_at,
        "stale": is_stale(age_seconds),
        "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
        "staleness_threshold_seconds": STALENESS_THRESHOLD_SECONDS,
    }
    return JSONResponse(response)


async def http_metrics(request):
    """Prometheus metrics endpoint using prometheus-client library"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    # These are injected by register_http_routes via request.state
    server_start_time = request.state._http_api_server_start_time
    server_version = request.state._http_api_server_version
    server_build_sha = getattr(request.state, "_http_api_server_build_sha", "unknown")

    try:
        # Update gauges with current values before generating output
        # Server info (static, set once)
        SERVER_INFO.labels(version=server_version, commit=server_build_sha).set(1)

        # Server uptime
        uptime_seconds = time.time() - server_start_time
        SERVER_UPTIME.set(uptime_seconds)

        # Agent metrics (from cached metadata — no DB call in handler path)
        try:
            from src.mcp_handlers.shared import get_mcp_server
            mcp_server = get_mcp_server()
            # Read already-loaded metadata dict; background tasks keep it fresh.
            # Do NOT call load_metadata_async() here — it awaits asyncpg.
            status_counts = {"active": 0, "paused": 0, "archived": 0, "waiting_input": 0, "deleted": 0}
            for meta in mcp_server.agent_metadata.values():
                status = getattr(meta, 'status', 'active')
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    status_counts["active"] += 1

            for status, count in status_counts.items():
                AGENTS_TOTAL.labels(status=status).set(count)
        except Exception as e:
            logger.debug(f"Could not load agent metrics: {e}")

        # Dialectic sessions (in-memory, no DB call)
        try:
            from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
            DIALECTIC_SESSIONS_ACTIVE.set(len(ACTIVE_SESSIONS))
        except Exception as e:
            logger.debug(f"Could not load dialectic metrics: {e}")

        # Generate Prometheus exposition format using the library
        output = generate_latest(REGISTRY)

        return Response(
            content=output,
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Error generating metrics: {e}", exc_info=True)
        return JSONResponse({
            "error": "Failed to generate metrics",
            "details": str(e)
        }, status_code=500)


# ---------------------------------------------------------------------------
# Debug: memory profiling (tracemalloc)
# ---------------------------------------------------------------------------

async def http_debug_memory(request):
    """Top memory allocations via tracemalloc (if enabled)."""
    # Debug endpoint — leaks internal paths/allocations; never serve it open.
    if not access._check_http_auth(request, http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN")):
        return access._http_unauthorized()
    import tracemalloc
    if not tracemalloc.is_tracing():
        return JSONResponse({"error": "tracemalloc not enabled"}, status_code=503)

    snapshot = tracemalloc.take_snapshot()
    # Filter out importlib/tracemalloc noise
    snapshot = snapshot.filter_traces([
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ])

    top_n = int(request.query_params.get("top", "25"))
    stats = snapshot.statistics("lineno")

    current, peak = tracemalloc.get_traced_memory()
    result = {
        "current_mb": round(current / 1024 / 1024, 1),
        "peak_mb": round(peak / 1024 / 1024, 1),
        "top_allocations": [
            {
                "file": str(stat.traceback),
                "size_mb": round(stat.size / 1024 / 1024, 2),
                "count": stat.count,
            }
            for stat in stats[:top_n]
        ],
    }

    # Also include monitor cache size
    try:
        from src.agent_monitor_state import monitors
        result["monitors_cached"] = len(monitors)
    except Exception:
        pass

    return JSONResponse(result)
