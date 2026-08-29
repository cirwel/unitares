"""
Phase A schema tests for surface-lease-plane migration 027.

Migration 027 (per RFC v0.8 §7.11.1) adds two tables:

  1. lease_plane.surface_kind_catalog — canonical registry of allowed scheme prefixes.
     Seeded by 027 with five schemes: capture, dialectic, file, resident, td.
     (`maintenance` arrives in 050 and `topic` in 069 — the catalog is an
     extension point, and later migrations adding to it is it working.)
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
    """Migration 027 creates surface_kind_catalog and seeds canonical schemes."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        rows = await conn.fetch(
            "SELECT surface_kind FROM lease_plane.surface_kind_catalog ORDER BY surface_kind"
        )
        kinds = {r["surface_kind"] for r in rows}
        # Subset, not equality. Equality here asserted "nobody has extended the
        # catalog", which is not a property 027 establishes and is the opposite
        # of what the catalog is for: it is an extension point, and 050 and 069
        # legitimately add to it. Because governance_test is SHARED, applying an
        # unmerged migration to it made this test fail on master and on every
        # branch at once — an early migration's test coupled to every later one.
        seeded_by_027 = {"capture", "dialectic", "file", "resident", "td"}
        missing = sorted(seeded_by_027 - kinds)
        assert not missing, (
            f"Migration 027 seeds missing from surface_kind_catalog: {missing} "
            f"(catalog holds {sorted(kinds)})"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_catalogued_kind_has_grammar_support():
    """No scheme may sit in the catalog without surface_id_grammar accepting it.

    This is the real invariant the old equality assertion was standing in for:
    it caught an unaccompanied catalog addition only indirectly, by the list
    drifting. Checking the relationship directly is both stronger — it fails
    for the actual defect rather than for any change — and immune to the
    shared-database coupling that made the equality version brittle.

    Deliberately one-directional: the grammar may be WIDER than the catalog
    (042 admits `agent:/`, which is not a catalogued surface kind), because a
    permissive grammar with a narrow catalog denies nothing. The dangerous
    direction is a catalogued kind the grammar would reject.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        kinds = [
            r["surface_kind"]
            for r in await conn.fetch(
                "SELECT surface_kind FROM lease_plane.surface_kind_catalog"
            )
        ]
        grammar = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'surface_id_grammar'
            """
        )
        assert grammar, "surface_id_grammar constraint is missing entirely"
        ungrammatical = sorted(k for k in kinds if f"{k}:" not in grammar)
        assert not ungrammatical, (
            "catalogued surface kinds with no surface_id_grammar prefix: "
            f"{ungrammatical} — a lease on one could never be created"
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
