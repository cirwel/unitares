"""R2 PR 1: storage layer for lineage lifecycle columns + helpers.

Covers migration 035 (column existence) plus the new backend helpers:
``declare_lineage``, ``demote_lineage``, ``archive_lineage``,
``increment_chain_obs_count``, ``stamp_lineage_eval``,
``are_lineages_provisional``.

See: docs/handoffs/2026-05-04-r2-implementation-plan.md PR 1
     docs/ontology/r2-honest-memory-integration.md §Storage
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg  # noqa: F401
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import can_connect_to_test_db

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Migration 035 — column existence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lineage_columns_exist(live_postgres_backend):
    async with live_postgres_backend.acquire() as conn:
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = 'core' AND table_name = 'identities'
               AND column_name IN (
                   'lineage_declared_at', 'lineage_demoted_at',
                   'lineage_archived_at', 'lineage_last_eval_at',
                   'chain_obs_count'
               )
             ORDER BY column_name
            """
        )
    names = {r["column_name"] for r in cols}
    assert names == {
        "lineage_declared_at",
        "lineage_demoted_at",
        "lineage_archived_at",
        "lineage_last_eval_at",
        "chain_obs_count",
    }
    chain_obs = next(r for r in cols if r["column_name"] == "chain_obs_count")
    assert chain_obs["data_type"] == "integer"
    assert chain_obs["is_nullable"] == "NO"
    assert chain_obs["column_default"] == "0"


@pytest.mark.asyncio
async def test_provisional_eval_index_exists(live_postgres_backend):
    """Sweeper-friendly partial index from migration 035."""
    async with live_postgres_backend.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = 'core'
               AND tablename = 'identities'
               AND indexname = 'idx_identities_provisional_eval'
            """
        )
    assert row is not None


# ---------------------------------------------------------------------------
# declare_lineage — idempotent stamp of lineage_declared_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declare_lineage_stamps_when_null(live_postgres_backend):
    from tests.db.conftest import _insert_identity, _cleanup, _uuid_suffix

    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    try:
        await _insert_identity(live_postgres_backend, parent_id)
        # Insert successor with parent set but DO NOT stamp lineage_declared_at
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')",
                successor_id,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, parent_agent_id) "
                "VALUES ($1, 'test-hash', $2)",
                successor_id, parent_id,
            )
        ok = await live_postgres_backend.declare_lineage(successor_id)
        assert ok
        async with live_postgres_backend.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT lineage_declared_at FROM core.identities WHERE agent_id = $1",
                successor_id,
            )
        assert row["lineage_declared_at"] is not None
    finally:
        await _cleanup(live_postgres_backend, [parent_id, successor_id])


@pytest.mark.asyncio
async def test_declare_lineage_idempotent(live_postgres_backend, seeded_pair):
    """Re-calling declare_lineage does not overwrite the original timestamp."""
    backend = live_postgres_backend
    async with backend.acquire() as conn:
        first = await conn.fetchval(
            "SELECT lineage_declared_at FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert first is not None
    ok = await backend.declare_lineage(seeded_pair.successor_id)
    assert ok
    async with backend.acquire() as conn:
        second = await conn.fetchval(
            "SELECT lineage_declared_at FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert second == first


# ---------------------------------------------------------------------------
# demote_lineage — provisional → demoted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demote_lineage_clears_parent_and_stamps_demoted_at(
    live_postgres_backend, seeded_pair,
):
    """provisional → demoted: parent_agent_id cleared, lineage_demoted_at set."""
    backend = live_postgres_backend
    ok = await backend.demote_lineage(
        seeded_pair.successor_id, reason="r1_unsupported",
    )
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT parent_agent_id, lineage_demoted_at, provisional_lineage "
            "FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert row["parent_agent_id"] is None
    assert row["lineage_demoted_at"] is not None
    assert row["provisional_lineage"] is False


@pytest.mark.asyncio
async def test_demote_lineage_returns_false_for_unknown_agent(
    live_postgres_backend,
):
    backend = live_postgres_backend
    ok = await backend.demote_lineage(
        "nonexistent-" + "x" * 8, reason="r1_unsupported",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# archive_lineage — grace expiration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_lineage_marks_archived_keeps_parent(
    live_postgres_backend, seeded_pair,
):
    """grace expiration: lineage_archived_at set, parent_agent_id retained but inert."""
    backend = live_postgres_backend
    ok = await backend.archive_lineage(seeded_pair.successor_id)
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT parent_agent_id, lineage_archived_at, provisional_lineage "
            "FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert row["parent_agent_id"] is not None  # retained as audit anchor
    assert row["lineage_archived_at"] is not None
    assert row["provisional_lineage"] is False


# ---------------------------------------------------------------------------
# increment_chain_obs_count — atomic forward-only counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_increment_chain_obs_count_is_atomic(
    live_postgres_backend, confirmed_pair,
):
    backend = live_postgres_backend
    new = await backend.increment_chain_obs_count(confirmed_pair.successor_id)
    assert new == 1
    new2 = await backend.increment_chain_obs_count(confirmed_pair.successor_id)
    assert new2 == 2


@pytest.mark.asyncio
async def test_increment_chain_obs_count_noop_for_provisional(
    live_postgres_backend, seeded_pair,
):
    """Counter only advances for confirmed lineage; provisional is no-op."""
    backend = live_postgres_backend
    new = await backend.increment_chain_obs_count(seeded_pair.successor_id)
    assert new == 0
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chain_obs_count FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert row["chain_obs_count"] == 0


@pytest.mark.asyncio
async def test_clawback_chain_counter_resets_to_zero(
    live_postgres_backend, confirmed_pair_with_obs,
):
    backend = live_postgres_backend
    ok = await backend.demote_lineage(
        confirmed_pair_with_obs.successor_id,
        reason="post_promotion_divergence",
    )
    assert ok
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chain_obs_count FROM core.identities WHERE agent_id = $1",
            confirmed_pair_with_obs.successor_id,
        )
    assert row["chain_obs_count"] == 0


# ---------------------------------------------------------------------------
# stamp_lineage_eval — cadence guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_lineage_eval_sets_timestamp(
    live_postgres_backend, seeded_pair,
):
    backend = live_postgres_backend
    await backend.stamp_lineage_eval(seeded_pair.successor_id)
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lineage_last_eval_at FROM core.identities WHERE agent_id = $1",
            seeded_pair.successor_id,
        )
    assert row["lineage_last_eval_at"] is not None


# ---------------------------------------------------------------------------
# are_lineages_provisional — batch primitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_are_lineages_provisional_batch(
    live_postgres_backend, seeded_pair, confirmed_pair,
):
    backend = live_postgres_backend
    out = await backend.are_lineages_provisional([
        seeded_pair.successor_id,
        confirmed_pair.successor_id,
        "missing-agent-id",
    ])
    assert out[seeded_pair.successor_id] is True
    assert out[confirmed_pair.successor_id] is False
    assert out["missing-agent-id"] is False


@pytest.mark.asyncio
async def test_are_lineages_provisional_empty_list(live_postgres_backend):
    out = await live_postgres_backend.are_lineages_provisional([])
    assert out == {}
