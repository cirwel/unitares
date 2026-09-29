"""Vigil panel endpoint and cycle statistics.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

from src.http_routes import access
from src.http_routes import residents

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Vigil panel endpoint
# ---------------------------------------------------------------------------

_VIGIL_DEFAULT_WINDOW_HOURS = 72
_VIGIL_DEFAULT_RECENT_LIMIT = 30
_VIGIL_CYCLE_HISTORY_LIMIT = 48  # ~24h at one cycle / 30min


def _vigil_agent_id(mcp_server_obj) -> Optional[str]:
    """Resolve the active Vigil agent_id from mcp_server agent_metadata.

    Mirrors the label->meta preference logic in ``http_residents`` (prefer
    active over archived; within same tier, prefer more total_updates) so
    the panel tracks the same Vigil row the residents strip shows.
    """
    best: Optional[tuple[str, Any]] = None
    for agent_id, meta in list(getattr(mcp_server_obj, "agent_metadata", {}).items()):
        label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
        if not label or label.lower() != "vigil":
            continue
        if best is None:
            best = (agent_id, meta)
            continue
        b_meta = best[1]
        b_active = getattr(b_meta, "status", None) == "active"
        n_active = getattr(meta, "status", None) == "active"
        if n_active and not b_active:
            best = (agent_id, meta)
            continue
        if b_active and not n_active:
            continue
        if (getattr(meta, "total_updates", 0) or 0) > \
           (getattr(b_meta, "total_updates", 0) or 0):
            best = (agent_id, meta)
    return best[0] if best else None


def _vigil_cycle_history(agent_id: str, window_hours: int,
                         limit: int = _VIGIL_CYCLE_HISTORY_LIMIT) -> list[dict]:
    """Flatten eisv_update events for Vigil into a cycle history for the panel.

    Each entry: ts, coherence, risk, verdict, E, I, S, V. Newest first.
    Pulls from the broadcaster ring buffer (~6h of fleet-wide events, longer
    for low-traffic residents like Vigil), clipped to ``window_hours``.
    """
    cutoff = time.time() - window_hours * 3600
    points: list[dict] = []
    for event in broadcaster_instance.event_history:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            continue
        if event.get("agent_id") != agent_id:
            continue
        ts_str = event.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        flat = residents._extract_eisv_fields(event)
        points.append({
            "timestamp": ts_str,
            "ts": ts,
            "E": flat.get("E"),
            "I": flat.get("I"),
            "S": flat.get("S"),
            "V": flat.get("V"),
            "coherence": flat.get("coherence"),
            "risk": flat.get("risk_score"),
            "verdict": flat.get("verdict"),
        })
    points.sort(key=lambda p: p["ts"], reverse=True)
    return points[:limit]


def _vigil_stats(cycles: list[dict], writes: list[dict]) -> dict:
    """Roll up cycle list + write list into summary metrics for the stat strip."""
    now_ts = time.time()
    last_cycle_ts = cycles[0]["ts"] if cycles else None
    last_cycle_iso = cycles[0]["timestamp"] if cycles else None

    def _within(hours, items, key="ts"):
        floor = now_ts - hours * 3600
        return sum(1 for it in items if (it.get(key) or 0) >= floor)

    cycles_24h = _within(24, cycles)
    writes_24h = 0
    for w in writes:
        ts_str = w.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= now_ts - 24 * 3600:
            writes_24h += 1

    coh_values = [c.get("coherence") for c in cycles
                  if isinstance(c.get("coherence"), (int, float))]
    avg_coh = (sum(coh_values) / len(coh_values)) if coh_values else None

    verdicts = [c.get("verdict") for c in cycles if c.get("verdict")]
    last_verdict = verdicts[0] if verdicts else None

    return {
        "last_cycle_at": last_cycle_iso,
        "last_cycle_age_seconds": (now_ts - last_cycle_ts) if last_cycle_ts else None,
        "cycles_24h": cycles_24h,
        "writes_24h": writes_24h,
        "avg_coherence_window": avg_coh,
        "last_verdict": last_verdict,
        "total_cycles_in_window": len(cycles),
        "total_writes_in_window": len(writes),
    }


async def http_vigil_summary(request):
    """GET /v1/vigil/summary — Vigil panel data.

    Vigil is a resident janitor that runs every 30min via launchd. Its KG
    writes are mostly low-severity groundskeeper deltas ("N stale, M archived")
    that crowd the main activity feed. This endpoint segregates them into a
    dedicated stream so the main feed can filter them out by default.

    Response shape::

        {
            "success": true,
            "agent_id": "...",
            "window_hours": 72,
            "stats": {"last_cycle_at": ..., "cycles_24h": N, "writes_24h": N, ...},
            "cycles": [{"timestamp": ..., "coherence": ..., "verdict": ..., ...}, ...],
            "recent_writes": [{"id": ..., "summary": ..., "severity": ..., ...}, ...],
            "generated_at": "..."
        }
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        window_hours = int(request.query_params.get("window_hours", _VIGIL_DEFAULT_WINDOW_HOURS))
    except ValueError:
        window_hours = _VIGIL_DEFAULT_WINDOW_HOURS
    window_hours = max(1, min(window_hours, 24 * 30))

    try:
        recent_limit = int(request.query_params.get("limit", _VIGIL_DEFAULT_RECENT_LIMIT))
    except ValueError:
        recent_limit = _VIGIL_DEFAULT_RECENT_LIMIT
    recent_limit = max(1, min(recent_limit, 200))

    from src.mcp_handlers.shared import lazy_mcp_server
    agent_id = _vigil_agent_id(lazy_mcp_server)

    if not agent_id:
        return JSONResponse({
            "success": True,
            "agent_id": None,
            "window_hours": window_hours,
            "stats": {},
            "cycles": [],
            "recent_writes": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "no Vigil agent found in metadata",
        })

    cycles = _vigil_cycle_history(agent_id, window_hours)
    writes = await residents._recent_writes_for_agent(agent_id, limit=recent_limit)
    stats = _vigil_stats(cycles, writes)

    return JSONResponse({
        "success": True,
        "agent_id": agent_id,
        "window_hours": window_hours,
        "stats": stats,
        "cycles": cycles,
        "recent_writes": writes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
