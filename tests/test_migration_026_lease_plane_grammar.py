"""
Phase A schema tests for surface-lease-plane migration 026.

Migration 026 (per RFC v0.8 §7.2.2 + §7.2.3) does three things to
lease_plane.surface_leases:

  1. Adds CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)')
     — storage-layer rejection of malformed scheme prefixes.
  2. Drops the regular `surface_kind` column.
  3. Re-adds `surface_kind` as GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED
     — surface_kind is now derived, not caller-supplied.

Pre-flight rule (per operator decision 2026-04-30, see Phase A plan):
the migration aborts with an explicit error message if surface_leases
contains any rows when 026 runs. This avoids silent destructive
DROP COLUMN against populated data.

 PR 1 row 1-3
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


async def _insert_lease(
    conn,
    *,
    surface_id: str,
    surface_kind: str | None = None,
    holder_kind: str = "remote_heartbeat",
    holder_class: str = "process_instance",
    heartbeat_required: bool = True,
    ttl_s: int = 60,
) -> uuid.UUID:
    """Insert a lease row directly. Returns the generated lease_id."""
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_s)
    lease_id = uuid.uuid4()
    # When surface_kind is None, omit it from the INSERT (post-026 generated column case).
    if surface_kind is None:
        await conn.execute(
            """
            INSERT INTO lease_plane.surface_leases
              (lease_id, surface_id, holder_agent_uuid, holder_kind,
               holder_class, heartbeat_required, original_ttl_s,
               acquired_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            lease_id, surface_id, holder_uuid, holder_kind,
            holder_class, heartbeat_required, ttl_s, now, expires,
        )
    else:
        await conn.execute(
            """
            INSERT INTO lease_plane.surface_leases
              (lease_id, surface_id, surface_kind, holder_agent_uuid, holder_kind,
               holder_class, heartbeat_required, original_ttl_s,
               acquired_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            lease_id, surface_id, surface_kind, holder_uuid, holder_kind,
            holder_class, heartbeat_required, ttl_s, now, expires,
        )
    return lease_id


@pytest.mark.asyncio
async def test_migration_026_grammar_check_rejects_invalid_scheme():
    """surface_id with a non-canonical scheme prefix is rejected at storage."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_lease(conn, surface_id="potato:/not_a_real_scheme")
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_026_grammar_check_accepts_canonical_schemes():
    """All five canonical schemes (file://, dialectic:/, resident:/, capture:/, td:/) are accepted."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        # Each scheme should INSERT cleanly.
        canonical = [
            "file:///tmp/test_grammar_file.py",
            "dialectic:/abcdef0123456789",
            "resident:/sentinel",
            "capture:/window_a,window_b",
            "td:/network/op1",
        ]
        for surface_id in canonical:
            await _insert_lease(conn, surface_id=surface_id)
        count = await conn.fetchval("SELECT count(*) FROM lease_plane.surface_leases")
        assert count == 5, f"Expected 5 rows after canonical inserts, got {count}"
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_026_surface_kind_is_generated_column():
    """surface_kind is a generated column derived from surface_id scheme prefix."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        # Verify is_generated metadata.
        is_gen = await conn.fetchval(
            """
            SELECT is_generated FROM information_schema.columns
            WHERE table_schema='lease_plane' AND table_name='surface_leases'
              AND column_name='surface_kind'
            """
        )
        assert is_gen == "ALWAYS", (
            f"surface_kind should be GENERATED ALWAYS after migration 026, got is_generated={is_gen!r}"
        )
        # Verify derivation: insert without surface_kind, read it back.
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        lease_id = await _insert_lease(conn, surface_id="dialectic:/derivation_check")
        derived_kind = await conn.fetchval(
            "SELECT surface_kind FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert derived_kind == "dialectic", (
            f"surface_kind should derive to 'dialectic' for 'dialectic:/...', got {derived_kind!r}"
        )
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_026_surface_kind_insert_with_explicit_value_raises():
    """Generated column rejects caller-supplied surface_kind at INSERT."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        # Generated columns reject INSERT with explicit value (Postgres error 42601 / 428C9).
        with pytest.raises(
            (asyncpg.GeneratedAlwaysError, asyncpg.PostgresSyntaxError, asyncpg.InvalidColumnReferenceError),
        ):
            await _insert_lease(
                conn,
                surface_id="file:///tmp/conflict_check.py",
                surface_kind="dialectic",  # caller tries to override; should fail
            )
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()
