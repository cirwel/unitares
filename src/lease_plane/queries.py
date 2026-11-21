"""Read-only SQL helpers for ``lease_plane.surface_leases``.

Owned by the ``lease_plane`` component so server callers (such as identity
presence and liveness checks) delegate surface-lease reads instead of embedding
foreign ``lease_plane.surface_leases`` SQL.
"""

from __future__ import annotations

from typing import Any


async def fetch_live_surface_lease(conn: Any, surface_id: str) -> Any:
    """Return the latest unreleased, unexpired row for ``surface_id`` (or None)."""
    return await conn.fetchrow(
        """
                SELECT lease_id::text AS lease_id, audit_session,
                       (last_heartbeat_at IS NOT NULL
                        AND last_heartbeat_at > acquired_at) AS renewed
                FROM lease_plane.surface_leases
                WHERE surface_id = $1
                  AND released_at IS NULL
                  AND expires_at > NOW()
                ORDER BY expires_at DESC
                LIMIT 1
                """,
        surface_id,
    )


async def has_live_surface_lease(conn: Any, surface_id: str) -> bool:
    """Return True if ``surface_id`` holds an unreleased, unexpired lease."""
    live = await conn.fetchval(
        """
                SELECT EXISTS(
                    SELECT 1 FROM lease_plane.surface_leases
                    WHERE surface_id = $1
                      AND released_at IS NULL
                      AND expires_at > NOW()
                )
                """,
        surface_id,
    )
    return bool(live)
