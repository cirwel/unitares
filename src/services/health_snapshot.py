"""
Cached health snapshot — shared between the deep_health_probe_task (writer)
and the health_check MCP handler / /health/deep REST endpoint (readers).

 for the design.

Rationale: the MCP SDK's anyio task group deadlocks asyncpg awaits inside
MCP tool handlers. Background tasks running on the main event loop can
`await` asyncpg safely. So we run the full health probe in a background
task and serve the result from an in-memory cache with no DB touches on
the read path.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

# Probe cadence & staleness contract. Override with UNITARES_HEALTH_PROBE_INTERVAL_SECONDS.
PROBE_INTERVAL_SECONDS = 30.0
STALENESS_THRESHOLD_SECONDS = 90.0  # 3x probe interval — flagged as stale above this

_snapshot: Optional[dict] = None
_snapshot_monotonic: Optional[float] = None
_snapshot_wall: Optional[float] = None
_lock = asyncio.Lock()


async def set_snapshot(data: dict) -> None:
    """Store a fresh snapshot. Called only by the probe task."""
    global _snapshot, _snapshot_monotonic, _snapshot_wall
    async with _lock:
        _snapshot = data
        _snapshot_monotonic = time.monotonic()
        _snapshot_wall = time.time()


def get_snapshot() -> Tuple[Optional[dict], Optional[float], Optional[float]]:
    """Read the most recent snapshot.

    Returns:
        (snapshot_dict, age_seconds, produced_at_wall_time)
        or (None, None, None) if no probe has run yet.

    Safe to call synchronously — writes are atomic dict replacements.
    """
    if _snapshot is None or _snapshot_monotonic is None:
        return None, None, None
    age = time.monotonic() - _snapshot_monotonic
    return _snapshot, age, _snapshot_wall


def is_stale(age_seconds: Optional[float]) -> bool:
    """Return True if the given age exceeds the staleness threshold."""
    if age_seconds is None:
        return True
    return age_seconds > STALENESS_THRESHOLD_SECONDS


def clear_snapshot() -> None:
    """Reset the cache. Test helper — do not call from production code."""
    global _snapshot, _snapshot_monotonic, _snapshot_wall
    _snapshot = None
    _snapshot_monotonic = None
    _snapshot_wall = None
