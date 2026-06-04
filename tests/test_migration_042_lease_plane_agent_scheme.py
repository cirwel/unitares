"""
Schema test for migration 042 — the `agent:/` ephemeral-agent presence scheme.

Migration 042 extends the migration-026 `surface_id_grammar` CHECK to allow
`agent:/` surface_ids. These are ephemeral-agent presence rows (one per agent,
routed to the remote_heartbeat pure-TTL-row path by the plane). This test pins
the storage-layer acceptance: an `agent:/...` surface_id INSERTs cleanly and
derives surface_kind='agent', while the prior canonical schemes still work and a
non-canonical scheme is still rejected.
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
async def test_migration_042_accepts_agent_scheme_and_derives_kind():
    """An agent:/ surface_id INSERTs cleanly and surface_kind derives to 'agent'."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        surface_id = "agent:/ag-7SDzA2Tm"
        lease_id = await _insert_lease(conn, surface_id=surface_id)
        kind = await conn.fetchval(
            "SELECT surface_kind FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert kind == "agent", f"expected surface_kind='agent', got {kind!r}"
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_042_still_rejects_non_canonical_scheme():
    """042 only ADDS agent:/ — a bogus scheme is still rejected by the grammar."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_lease(conn, surface_id="potato:/still_not_real")
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()
