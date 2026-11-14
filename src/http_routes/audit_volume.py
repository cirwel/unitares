"""Read-only HTTP route for 24h audit-event volume anomaly checks.

Mounted in Core (`src/http_api.py`) at `GET /v1/audit/volume-summary` and gated
by `access._check_http_auth` so remote Sentinel residents (`elixir/sentinel`)
never open direct PostgreSQL connections.
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse

from src.http_routes import access
from src.logging_utils import get_logger

logger = get_logger(__name__)

DEFAULT_FLOOR = 300
MIN_FLOOR = 0
MAX_FLOOR = 10_000_000


async def http_get_audit_volume_summary(request):
    """GET /v1/audit/volume-summary?floor=300 — weighted 24h audit volume summary."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    raw_floor = request.query_params.get("floor")
    if raw_floor is None or str(raw_floor).strip() == "":
        floor = DEFAULT_FLOOR
    else:
        try:
            floor = int(str(raw_floor).strip())
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "'floor' must be an integer"},
                status_code=400,
            )

    if floor < MIN_FLOOR or floor > MAX_FLOOR:
        return JSONResponse(
            {
                "success": False,
                "error": f"'floor' must be between {MIN_FLOOR} and {MAX_FLOOR}",
            },
            status_code=400,
        )

    from src.db.mixins.audit import query_audit_volume_summary

    try:
        rows = await query_audit_volume_summary(floor=floor)
        return JSONResponse({"success": True, "floor": floor, "rows": rows})
    except Exception as exc:
        logger.error("Error querying audit volume summary: %s", exc)
        return JSONResponse(
            {"success": False, "error": "Failed to query audit volume summary"},
            status_code=500,
        )
