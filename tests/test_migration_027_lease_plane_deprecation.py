"""
Phase A schema tests for surface-lease-plane migration 027.

Migration 027 (per RFC v0.8 §7.11.1) adds two tables:

  1. lease_plane.surface_kind_catalog — canonical registry of allowed scheme prefixes.
     Migration 027 seeds the 5 v0 schemes (file, dialectic, resident, capture, td).
  2. lease_plane.deprecated_schemes — first-class persistence substrate for
     §7.11 deprecation procedure. FK to surface_kind_catalog so deprecation
     can only target a registered kind.

 PR 1 row 4-5
"""

from __future__ import annotations

import sys
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


@pytest.mark.asyncio
async def test_migration_027_surface_kind_catalog_seeded():
    """Migration 027 creates surface_kind_catalog and seeds the 5 v0 schemes."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        rows = await conn.fetch(
            "SELECT surface_kind FROM lease_plane.surface_kind_catalog ORDER BY surface_kind"
        )
        kinds = [r["surface_kind"] for r in rows]
        expected_v0 = {"capture", "dialectic", "file", "resident", "td"}
        assert expected_v0 <= set(kinds), (
            f"Expected v0 canonical schemes seeded, got {kinds}"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_027_deprecated_schemes_table_exists():
    """deprecated_schemes table exists with the v0.8 §7.11.1 schema."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        # Required columns per RFC v0.8 §7.11.1.
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'lease_plane'
              AND table_name = 'deprecated_schemes'
            ORDER BY ordinal_position
            """
        )
        col_names = [c["column_name"] for c in cols]
        required = {
            "surface_kind",
            "deprecation_id",
            "marked_deprecated_at",
            "marked_by_session_id",
            "drain_window_days",
            "sweep_started_at",
            "sweep_completed_at",
            "check_migrated_at",
        }
        missing = required - set(col_names)
        assert not missing, (
            f"deprecated_schemes missing required columns from RFC v0.8 §7.11.1: {missing}"
        )

        # surface_kind is the PK and FK to surface_kind_catalog.
        # Verify FK by attempting an INSERT with an unknown kind — should raise FK violation.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                """
                INSERT INTO lease_plane.deprecated_schemes
                  (surface_kind, marked_by_session_id)
                VALUES ('not_a_real_scheme', 'test-session-fk-check')
                """
            )

        # drain_window_days CHECK: > 0 AND <= 90.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO lease_plane.deprecated_schemes
                  (surface_kind, marked_by_session_id, drain_window_days)
                VALUES ('td', 'test-session-window-check', 0)
                """
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO lease_plane.deprecated_schemes
                  (surface_kind, marked_by_session_id, drain_window_days)
                VALUES ('td', 'test-session-window-check', 91)
                """
            )
    finally:
        # Cleanup: remove any test rows we inserted (FK + CHECK violations roll back, but be safe).
        await conn.execute(
            "DELETE FROM lease_plane.deprecated_schemes WHERE marked_by_session_id LIKE 'test-session-%'"
        )
        await conn.close()
