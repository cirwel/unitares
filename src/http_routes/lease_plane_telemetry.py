"""Read-only HTTP routes for lease-plane alarm polling and Phase-B evaluation.

Mounted in Core (`src/http_api.py`) under `/v1/lease-plane/*` and gated by
`access._check_http_auth` so remote monitoring clients never open direct
PostgreSQL connections.
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse

from src.http_routes import access
from src.logging_utils import get_logger

logger = get_logger(__name__)

MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 365


async def http_get_lease_alarm_events(request):
    """GET /v1/lease-plane/alarm-events?since=<iso> — read-only alarm rows."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    from src.lease_plane_telemetry import fetch_lease_alarm_events, parse_since_param

    try:
        since = parse_since_param(request.query_params.get("since"))
    except ValueError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)

    try:
        payload = await fetch_lease_alarm_events(since=since)
        return JSONResponse({"success": True, **payload})
    except Exception as exc:
        logger.error(f"Error fetching lease-plane alarm events: {exc}")
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


async def http_get_lease_phase_b(request):
    """GET /v1/lease-plane/phase-b/{surface_kind}?window_days=14&accept_drill_evidence=false."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    from src.lease_plane_phase_b import UnknownSurfaceKindError, evaluate_phase_b_async

    surface_kind = str(request.path_params.get("surface_kind") or "").strip()
    raw_window = request.query_params.get("window_days", "14")
    try:
        window_days = int(raw_window)
    except (TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "'window_days' must be an integer"},
            status_code=400,
        )
    if window_days < MIN_WINDOW_DAYS or window_days > MAX_WINDOW_DAYS:
        return JSONResponse(
            {
                "success": False,
                "error": f"'window_days' must be between {MIN_WINDOW_DAYS} and {MAX_WINDOW_DAYS}",
            },
            status_code=400,
        )

    raw_drill = str(request.query_params.get("accept_drill_evidence", "false")).strip().lower()
    accept_drill = raw_drill in {"1", "true", "yes"}

    try:
        report = await evaluate_phase_b_async(
            surface_kind=surface_kind,
            window_days=window_days,
            accept_drill_evidence=accept_drill,
        )
        return JSONResponse({"success": True, **report.to_dict()})
    except UnknownSurfaceKindError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error(f"Error evaluating lease-plane Phase B for {surface_kind!r}: {exc}")
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)
