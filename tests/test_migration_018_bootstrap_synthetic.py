"""
Phase 1 schema tests for onboard-bootstrap-checkin (migration 018).

Verifies the column, partial index, unique-partial index, and matview
projection are in place. Subsequent phases add handler tests, filter-site
tests, and the bootstrapped-but-silent observable surface.

Spec: §3.2, §3.4, §9 step 1.
"""

from __future__ import annotations

import sys
import uuid
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


async def _seed_identity(conn) -> int:
    """Insert a fresh agent + identity, return the identity_id."""
    agent_id = f"test-{uuid.uuid4()}"
    await conn.execute(
        "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')",
        agent_id,
    )
    identity_id = await conn.fetchval(
        """
        INSERT INTO core.identities (agent_id, api_key_hash)
        VALUES ($1, 'test-hash')
        RETURNING identity_id
        """,
        agent_id,
    )
    return identity_id


@pytest.mark.asyncio
async def test_synthetic_column_exists_with_default():
    """The `synthetic` column exists, NOT NULL, defaults to false."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        col = await conn.fetchrow(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'core'
              AND table_name = 'agent_state'
              AND column_name = 'synthetic'
            """
        )
    finally:
        await conn.close()

    assert col is not None, "core.agent_state.synthetic column missing"
    assert col["data_type"] == "boolean"
    assert col["is_nullable"] == "NO"
    assert col["column_default"] == "false"


@pytest.mark.asyncio
async def test_existing_inserts_default_to_synthetic_false():
    """Inserts that omit `synthetic` get false (existing call sites unaffected)."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        identity_id = await _seed_identity(conn)
        await conn.execute(
            """
            INSERT INTO core.agent_state (identity_id, entropy, integrity)
            VALUES ($1, 0.4, 0.6)
            """,
            identity_id,
        )
        synthetic = await conn.fetchval(
            "SELECT synthetic FROM core.agent_state WHERE identity_id = $1",
            identity_id,
        )
    finally:
        await conn.close()

    assert synthetic is False


@pytest.mark.asyncio
async def test_partial_index_exists():
    """idx_agent_state_synthetic_partial covers measured-state queries."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        idx = await conn.fetchrow(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'core'
              AND tablename = 'agent_state'
              AND indexname = 'idx_agent_state_synthetic_partial'
            """
        )
    finally:
        await conn.close()

    assert idx is not None, "partial index for measured-state queries missing"
    assert "WHERE (synthetic = false)" in idx["indexdef"]
    assert "identity_id" in idx["indexdef"]
    assert "recorded_at" in idx["indexdef"]


@pytest.mark.asyncio
async def test_unique_partial_index_enforces_one_bootstrap_per_identity():
    """Two synthetic rows for one identity violate uq_agent_state_one_bootstrap_per_identity."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        identity_id = await _seed_identity(conn)

        await conn.execute(
            """
            INSERT INTO core.agent_state (identity_id, entropy, integrity, synthetic)
            VALUES ($1, 0.5, 0.5, true)
            """,
            identity_id,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO core.agent_state (identity_id, entropy, integrity, synthetic)
                VALUES ($1, 0.6, 0.4, true)
                """,
                identity_id,
            )

        # But many measured rows for the same identity stay legal.
        for _ in range(3):
            await conn.execute(
                """
                INSERT INTO core.agent_state (identity_id, entropy, integrity, synthetic)
                VALUES ($1, 0.4, 0.7, false)
                """,
                identity_id,
            )

        synthetic_count = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agent_state WHERE identity_id = $1 AND synthetic = true",
            identity_id,
        )
        measured_count = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agent_state WHERE identity_id = $1 AND synthetic = false",
            identity_id,
        )
    finally:
        await conn.close()

    assert synthetic_count == 1
    assert measured_count == 3


@pytest.mark.asyncio
async def test_matview_projects_synthetic_column():
    """mv_latest_agent_states exposes `synthetic` so the dashboard can filter."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        # Materialized view columns aren't in information_schema; use pg_attribute.
        col = await conn.fetchrow(
            """
            SELECT a.attname, t.typname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE n.nspname = 'core'
              AND c.relname = 'mv_latest_agent_states'
              AND a.attname = 'synthetic'
              AND a.attnum > 0
            """
        )

        # And REFRESH CONCURRENTLY still works (depends on the unique index).
        await conn.execute(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY core.mv_latest_agent_states"
        )
    finally:
        await conn.close()

    assert col is not None, "matview must project synthetic column for dashboard filtering"
    assert col["typname"] == "bool"


@pytest.mark.asyncio
async def test_migration_018_registered():
    """Migration row landed in core.schema_migrations."""
    await ensure_test_database_schema()

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT version, name FROM core.schema_migrations WHERE version = 18"
        )
    finally:
        await conn.close()

    assert row is not None
    assert row["name"] == "bootstrap_synthetic_state"
