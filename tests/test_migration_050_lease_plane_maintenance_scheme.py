"""
Schema test for migration 050 — the `maintenance:/` cleanup/repair scheme.

Migration 050 extends the lease-plane `surface_id_grammar` CHECK to allow
`maintenance:/` surface_ids and registers the surface kind. These surfaces are
for maintenance jobs, not resident lifecycle/presence rows. The generated
`surface_kind` column derives `maintenance` from the scheme prefix.
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


async def _insert_lease(conn, *, surface_id: str, ttl_s: int = 60) -> uuid.UUID:
    """Insert a remote_heartbeat lease row directly. Returns the lease_id."""
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_s)
    lease_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind,
           holder_class, heartbeat_required, original_ttl_s,
           acquired_at, expires_at)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, $4, $5, $6)
        """,
        lease_id, surface_id, holder_uuid, ttl_s, now, expires,
    )
    return lease_id


@pytest.mark.asyncio
async def test_migration_050_accepts_maintenance_scheme_and_derives_kind():
    """A maintenance:/ surface_id INSERTs cleanly and derives surface_kind."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        surface_id = "maintenance:/worktree_reaper"
        lease_id = await _insert_lease(conn, surface_id=surface_id)
        kind = await conn.fetchval(
            "SELECT surface_kind FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert kind == "maintenance", f"expected surface_kind='maintenance', got {kind!r}"
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_050_still_rejects_non_canonical_scheme():
    """050 only adds maintenance:/; a bogus scheme is still rejected."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_lease(conn, surface_id="potato:/still_not_real")
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()
