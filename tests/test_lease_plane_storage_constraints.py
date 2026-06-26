"""Storage-layer gate tests for surface_leases (RFC v0.8 §7.2 / §9).

Pins two §9 named gates against the live `lease_plane.surface_leases`
table — both are storage-layer behaviors set up by migration 026:

  - `surface_id_grammar` CHECK constraint rejects unknown schemes.
  - `surface_kind` generated column derives from the surface_id prefix.

      db/postgres/migrations/026_lease_plane_grammar.sql
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


_INSERT = """
INSERT INTO lease_plane.surface_leases
  (lease_id, surface_id, holder_agent_uuid, holder_kind, holder_class,
   heartbeat_required, original_ttl_s, acquired_at, expires_at, intent)
VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, $4, $5, $6, $7)
"""


async def _cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM lease_plane.surface_leases WHERE intent LIKE 'storage-gate-test%'"
    )


async def _insert_lease(conn, *, surface_id: str, intent: str) -> uuid.UUID:
    lease_id = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        _INSERT,
        lease_id, surface_id, uuid.uuid4(), 60, now,
        now + timedelta(seconds=60), intent,
    )
    return lease_id


@pytest.mark.asyncio
async def test_invalid_uri_scheme_rejected_at_storage():
    """RFC §7.2 / §9 — INSERT with `surface_id='not_a_scheme:foo'` raises a
    CHECK violation against the `surface_id_grammar` constraint added by
    migration 026.

    Pins the §9 named gate `test_invalid_uri_scheme_rejected_at_storage`."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="not_a_scheme:foo",
                intent="storage-gate-test-invalid-scheme",
            )
        assert "surface_id_grammar" in str(exc_info.value), (
            f"Expected CHECK violation to name surface_id_grammar; got: {exc_info.value}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_surface_kind_derived_from_scheme():
    """RFC §7.2.3 / §9 — `surface_kind` is a generated column. INSERT with
    `surface_id='file:///x.py'` produces `surface_kind='file'` automatically;
    the caller cannot supply a conflicting value because `surface_kind` is
    no longer a writable column.

    Pins the §9 named gate `test_surface_kind_derived_from_scheme`."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        cases = [
            ("file:///tmp/storage_gate_x.py", "file"),
            ("dialectic:/storage-gate-y", "dialectic"),
            ("maintenance:/storage-gate-maintenance", "maintenance"),
            ("td:/storage-gate-z", "td"),
        ]
        for sid, expected_kind in cases:
            lease_id = await _insert_lease(
                conn, surface_id=sid, intent=f"storage-gate-test-derived-{expected_kind}",
            )
            row = await conn.fetchrow(
                "SELECT surface_kind FROM lease_plane.surface_leases WHERE lease_id = $1",
                lease_id,
            )
            assert row is not None, f"row not found for {sid!r}"
            assert row["surface_kind"] == expected_kind, (
                f"surface_kind for {sid!r} expected {expected_kind!r}, got {row['surface_kind']!r}"
            )
    finally:
        await _cleanup(conn)
        await conn.close()
