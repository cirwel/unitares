"""Generic metric-series ingest/query API: /v1/metrics*, /v1/progress_flat.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
from datetime import datetime

from starlette.responses import JSONResponse


from src.logging_utils import get_logger

from src.http_routes import access

logger = get_logger(__name__)


async def http_post_metric(request):
    """POST /v1/metrics — write one `(name, value)` point into `metrics.series`.

    Body: `{"name": "...", "value": 1.23, "ts"?: "2026-04-20T..."}`
    Name must be registered in `src.fleet_metrics.catalog`; a leaked bearer
    token therefore cannot inject arbitrary metric names.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "Body must be a JSON object"}, status_code=400)

        name = payload.get("name")
        value = payload.get("value")
        ts_raw = payload.get("ts")
        if not isinstance(name, str) or not name:
            return JSONResponse({"success": False, "error": "Missing or invalid 'name'"}, status_code=400)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return JSONResponse({"success": False, "error": "Missing or invalid 'value' (number required)"}, status_code=400)

        ts = None
        if ts_raw is not None:
            if not isinstance(ts_raw, str):
                return JSONResponse({"success": False, "error": "'ts' must be ISO8601 string"}, status_code=400)
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                return JSONResponse({"success": False, "error": "'ts' is not valid ISO8601"}, status_code=400)

        from src.fleet_metrics import record
        try:
            await record(name, float(value), ts=ts)
        except KeyError as exc:
            return JSONResponse(
                {"success": False, "error": str(exc)},
                status_code=404,
            )
        return JSONResponse({"success": True}, status_code=201)
    except Exception as e:
        logger.error(f"Error recording metric: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_get_metrics(request):
    """GET /v1/metrics?name=...&since=...&until=...&limit=... — return a series."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        params = request.query_params
        name = params.get("name")
        if not name:
            return JSONResponse({"success": False, "error": "'name' query param required"}, status_code=400)

        def _parse_ts(raw: str | None):
            if raw is None:
                return None
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return "INVALID"

        since = _parse_ts(params.get("since"))
        until = _parse_ts(params.get("until"))
        if since == "INVALID" or until == "INVALID":
            return JSONResponse({"success": False, "error": "'since'/'until' must be ISO8601"}, status_code=400)

        try:
            limit = int(params.get("limit", "10000"))
        except ValueError:
            return JSONResponse({"success": False, "error": "'limit' must be integer"}, status_code=400)

        from src.fleet_metrics import query
        points = await query(name=name, since=since, until=until, limit=limit)
        return JSONResponse({
            "success": True,
            "name": name,
            "points": [{"ts": p.ts.isoformat(), "value": p.value} for p in points],
            "count": len(points),
        })
    except Exception as e:
        logger.error(f"Error querying metrics: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_get_metrics_catalog(request):
    """GET /v1/metrics/catalog — list all registered metric names and descriptions."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    from src.fleet_metrics import catalog as _catalog
    from src.fleet_metrics.storage import latest_ts_for_names
    metrics = sorted(_catalog.values(), key=lambda x: x.name)
    # last_point_ts lets the dashboard suppress empty `.error` twins
    # without firing a per-name probe — see dashboard/redesign/sections/metrics.js.
    try:
        last_ts = await latest_ts_for_names([m.name for m in metrics])
    except Exception as e:
        logger.warning(f"metrics catalog: latest_ts probe failed: {e}")
        last_ts = {}
    return JSONResponse({
        "success": True,
        "metrics": [
            {
                "name": m.name,
                "description": m.description,
                "unit": m.unit,
                "last_point_ts": last_ts[m.name].isoformat() if m.name in last_ts else None,
            }
            for m in metrics
        ],
    })


async def http_get_progress_flat_recent(request):
    """GET /v1/progress_flat/recent?hours=24 — latest snapshot per
    configured resident plus the probe-self row.
    """
    import json as _json
    from src.db import get_db
    from src.resident_progress.registry import RESIDENT_PROGRESS_REGISTRY
    from src.resident_progress.status import resolve_status

    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        hours = int(request.query_params.get("hours", "24"))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 168))  # clamp to [1, 168]

    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (resident_label)
                   resident_label, resident_uuid::text AS resident_uuid,
                   ticked_at, source, metric_value, window_seconds,
                   threshold, metric_below_threshold, heartbeat_alive,
                   candidate, suppressed_reason, error_details,
                   liveness_inputs, loop_detector_state
            FROM progress_flat_snapshots
            WHERE ticked_at > now() - make_interval(hours => $1)
            ORDER BY resident_label, ticked_at DESC
            """,
            hours,
        )

    by_label: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        # Coerce types for JSON
        if d.get("ticked_at") is not None:
            d["ticked_at"] = d["ticked_at"].isoformat()
        # error_details / liveness_inputs / loop_detector_state may arrive
        # as JSON-serialized strings (asyncpg jsonb default) or dicts
        # depending on connection-pool init. Normalize to dict-or-None.
        for jk in ("error_details", "liveness_inputs", "loop_detector_state"):
            v = d.get(jk)
            if isinstance(v, str):
                try:
                    d[jk] = _json.loads(v)
                except Exception:
                    pass
        by_label[r["resident_label"]] = d

    out = []
    for label in list(RESIDENT_PROGRESS_REGISTRY) + ["progress_flat_probe"]:
        r = by_label.get(label)
        if r is None:
            out.append({
                "resident_label": label,
                "status": "unresolved",
                "metric_value": None,
                "threshold": None,
                "window_seconds": None,
                "ticked_at": None,
            })
            continue
        r["status"] = resolve_status(r)
        out.append(r)

    return JSONResponse({"success": True, "rows": out})
