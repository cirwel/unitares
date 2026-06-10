"""
Schema tests for migrations 043/044 — the Wave 3 §8.1 shadow tables.

core.identities_shadow / core.agents_shadow are write-only audit replicas
created with `LIKE <canonical> INCLUDING ALL` plus a `shadow_write_at`
timestamp. These tests pin the §8.1 contract: column parity with canonical
(modulo shadow_write_at), no foreign keys (deliberate — see migration
headers), the comparator's unique join keys copied, and inserts accepted.
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

PAIRS = (("identities", "identities_shadow"), ("agents", "agents_shadow"))

_SHAPE_SQL = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'core' AND table_name = $1
    ORDER BY ordinal_position
"""


@pytest.mark.asyncio
async def test_shadow_tables_column_parity_with_canonical():
    """Shadow shape == canonical shape + trailing shadow_write_at. This is the
    same invariant db/postgres/schema_drift_check.sh gates operationally."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for canonical, shadow in PAIRS:
            canon_cols = [
                (r["column_name"], r["data_type"])
                for r in await conn.fetch(_SHAPE_SQL, canonical)
            ]
            shadow_cols = [
                (r["column_name"], r["data_type"])
                for r in await conn.fetch(_SHAPE_SQL, shadow)
            ]
            assert canon_cols, f"core.{canonical} missing from test schema"
            assert shadow_cols, f"core.{shadow} missing — migrations 043/044 not applied"
            assert shadow_cols[-1] == ("shadow_write_at", "timestamp with time zone"), (
                f"core.{shadow} must end with shadow_write_at timestamptz, "
                f"got {shadow_cols[-1]}"
            )
            assert shadow_cols[:-1] == canon_cols, (
                f"core.{shadow} drifted from core.{canonical}: "
                f"{set(shadow_cols[:-1]) ^ set(canon_cols)}"
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_shadow_write_at_not_null_with_default():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for _, shadow in PAIRS:
            row = await conn.fetchrow(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'core' AND table_name = $1
                  AND column_name = 'shadow_write_at'
                """,
                shadow,
            )
            assert row is not None, f"core.{shadow}.shadow_write_at missing"
            assert row["is_nullable"] == "NO"
            assert row["column_default"] is not None, (
                "shadow_write_at needs a now() default so the shadow writer "
                "never has to supply it"
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_shadow_tables_have_no_foreign_keys():
    """§8.1 FK decision: shadows are write-only audit replicas, no FKs.
    LIKE ... INCLUDING ALL does not copy FKs; this pins that nobody adds one."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for _, shadow in PAIRS:
            fk_count = await conn.fetchval(
                """
                SELECT count(*) FROM pg_constraint
                WHERE conrelid = ('core.' || $1)::regclass AND contype = 'f'
                """,
                shadow,
            )
            assert fk_count == 0, f"core.{shadow} must not carry FK constraints"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_identities_shadow_copied_unique_agent_id_index():
    """The §8.2 comparator full-outer-joins USING (agent_id); the canonical
    unique index must be copied to the shadow by INCLUDING ALL."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        defs = [
            r["indexdef"]
            for r in await conn.fetch(
                """
                SELECT indexdef FROM pg_indexes
                WHERE schemaname = 'core' AND tablename = 'identities_shadow'
                """
            )
        ]
        assert any(
            "UNIQUE" in d and "(agent_id)" in d for d in defs
        ), f"no unique agent_id index on core.identities_shadow; have: {defs}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_shadow_tables_accept_writer_shaped_inserts():
    """A row shaped like the shadow writer's output (explicit join key,
    defaults for the rest) inserts cleanly into both shadows."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    marker = f"shadow-test-{uuid.uuid4().hex[:12]}"
    try:
        await conn.execute(
            """
            INSERT INTO core.identities_shadow (identity_id, agent_id, api_key_hash)
            VALUES (
                (SELECT COALESCE(max(identity_id), 0) + 1000000 FROM core.identities_shadow),
                $1,
                'shadow-test-hash'
            )
            """,
            marker,
        )
        await conn.execute(
            "INSERT INTO core.agents_shadow (id, api_key) VALUES ($1, 'shadow-test-key')",
            marker,
        )
        got = await conn.fetchval(
            "SELECT shadow_write_at IS NOT NULL FROM core.identities_shadow WHERE agent_id = $1",
            marker,
        )
        assert got is True
    finally:
        await conn.execute(
            "DELETE FROM core.identities_shadow WHERE agent_id = $1", marker
        )
        await conn.execute("DELETE FROM core.agents_shadow WHERE id = $1", marker)
        await conn.close()
