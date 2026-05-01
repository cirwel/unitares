"""
PR 5 — migration 029 restores the `earned_status` immutability guard
that migration 028 silently dropped (council BLOCK from dialectic voice).

Migration 025 originally guarded 8 fields against UPDATE. Migration 028 was
authored to drop the surface_kind guard (it became a generated column post-026)
but in the rewrite the earned_status guard was also dropped — collateral
damage. This lets any UPDATE silently flip the substrate-earned promotion
flag from 'provisional' to 'earned' without the migration 025 anticipated
for that promotion.

Migration 029 restores the earned_status check and pins the contract via test.

Spec: docs/proposals/surface-lease-plane-v0.md §7.8 (substrate-earned identity)
      docs/proposals/surface-lease-plane-phase-a-plan.md PR 5
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _seed_lease(conn) -> uuid.UUID:
    lease_id = uuid.uuid4()
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind, holder_class,
           heartbeat_required, original_ttl_s, acquired_at, expires_at)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, 60, $4, $5)
        """,
        lease_id, "td:/pr5_earned_guard_test", holder_uuid, now,
        now + timedelta(seconds=60),
    )
    return lease_id


@pytest.mark.asyncio
async def test_migration_029_blocks_earned_status_update():
    """earned_status must be immutable per lease_id (substrate-earned promotion
    requires explicit migration, not silent UPDATE)."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            "DELETE FROM lease_plane.surface_leases WHERE surface_id = 'td:/pr5_earned_guard_test'"
        )
        lease_id = await _seed_lease(conn)
        # Verify default state: provisional.
        es = await conn.fetchval(
            "SELECT earned_status FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert es == "provisional"
        # Attempt to flip earned_status — should raise via the trigger.
        with pytest.raises(asyncpg.RaiseError) as exc_info:
            await conn.execute(
                "UPDATE lease_plane.surface_leases SET earned_status = 'earned' WHERE lease_id = $1",
                lease_id,
            )
        # Error message should be specific about earned_status.
        assert "earned_status" in str(exc_info.value).lower()
    finally:
        await conn.execute(
            "DELETE FROM lease_plane.surface_leases WHERE surface_id = 'td:/pr5_earned_guard_test'"
        )
        await conn.close()


@pytest.mark.asyncio
async def test_migration_029_still_allows_release_update():
    """Migration 029 restores ONLY the earned_status guard — sweep UPDATE
    (released_at, release_reason) must still succeed."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            "DELETE FROM lease_plane.surface_leases WHERE surface_id = 'td:/pr5_release_path'"
        )
        lease_id = await _seed_lease_with_sid(conn, "td:/pr5_release_path")
        # Sweep-style UPDATE must succeed (the post-028 PR 3a sweep path).
        await conn.execute(
            "UPDATE lease_plane.surface_leases SET released_at = now(), release_reason = 'forced' "
            "WHERE lease_id = $1",
            lease_id,
        )
        rel = await conn.fetchval(
            "SELECT released_at FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert rel is not None
    finally:
        await conn.execute(
            "DELETE FROM lease_plane.surface_leases WHERE surface_id = 'td:/pr5_release_path'"
        )
        await conn.close()


async def _seed_lease_with_sid(conn, sid: str) -> uuid.UUID:
    lease_id = uuid.uuid4()
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind, holder_class,
           heartbeat_required, original_ttl_s, acquired_at, expires_at)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, 60, $4, $5)
        """,
        lease_id, sid, holder_uuid, now, now + timedelta(seconds=60),
    )
    return lease_id
