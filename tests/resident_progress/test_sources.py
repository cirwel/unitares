from __future__ import annotations

from datetime import timedelta

import pytest

from src.resident_progress.sources import (
    ResidentProgressSource,
    KnowledgeDiscoverySource,
    EISVSyncSource,
    MetricsSeriesSource,
    CheckinSource,
    CHRONICLER_SERIES_NAMES,
)


@pytest.mark.asyncio
async def test_kg_source_returns_zero_for_unknown_uuid(test_db):
    src = KnowledgeDiscoverySource(test_db)
    out = await src.fetch(["00000000-0000-0000-0000-000000000000"], timedelta(hours=1))
    assert out == {"00000000-0000-0000-0000-000000000000": 0}


@pytest.mark.asyncio
async def test_kg_source_counts_recent_rows(test_db):
    uuid = "10000000-0000-0000-0000-000000000001"
    async with test_db.acquire() as conn:
        await conn.execute(
            "INSERT INTO knowledge.discoveries (id, agent_id, type, summary) "
            "VALUES ($1, $2, 'note', 'x') ON CONFLICT (id) DO NOTHING",
            "test-row-task4-1", uuid,
        )
    src = KnowledgeDiscoverySource(test_db)
    out = await src.fetch([uuid], timedelta(hours=1))
    assert out[uuid] >= 1


@pytest.mark.asyncio
async def test_kg_source_batches_one_query_for_many_uuids(test_db):
    seen_calls = []
    real_acquire = test_db.acquire

    class _Tracking:
        def __init__(self, c):
            self._c = c

        async def fetch(self, *args, **kwargs):
            seen_calls.append(args[0] if args else "")
            return await self._c.fetch(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._c, name)

    class _AcquireProxy:
        def __init__(self):
            pass

        async def __aenter__(self):
            self._cm = real_acquire()
            conn = await self._cm.__aenter__()
            return _Tracking(conn)

        async def __aexit__(self, *a):
            return await self._cm.__aexit__(*a)

    class _PoolProxy:
        """Thin wrapper around the real pool that intercepts acquire() calls."""

        def acquire(self):
            return _AcquireProxy()

        def __getattr__(self, name):
            return getattr(test_db, name)

    src = KnowledgeDiscoverySource(_PoolProxy())
    await src.fetch(
        [f"22222222-0000-0000-0000-{i:012d}" for i in range(5)],
        timedelta(hours=1),
    )
    assert len(seen_calls) == 1, "must issue exactly one batched query"


@pytest.mark.asyncio
async def test_eisv_sync_source_filters_by_event_type(test_db):
    src = EISVSyncSource(test_db)
    out = await src.fetch(["33333333-0000-0000-0000-000000000003"], timedelta(minutes=30))
    # No matching rows in test DB → returns zero, not raises
    assert out == {"33333333-0000-0000-0000-000000000003": 0}


def test_chronicler_series_names_includes_tokei():
    assert "tokei.unitares.src.code" in CHRONICLER_SERIES_NAMES


def test_chronicler_series_names_are_all_backed_by_a_scraper():
    """Every name in CHRONICLER_SERIES_NAMES must be a key Chronicler actually
    writes (a SCRAPERS entry). The two lists are hand-kept in sync via a comment
    in sources.py; without this guard a rename/removal on the Chronicler side
    leaves MetricsSeriesSource querying a series name nobody writes, which
    returns 0 forever with no error — the silent-zero failure the comment warns
    about.

    Subset, not equality: SCRAPERS also contains the github.* traffic series,
    which are deliberately excluded from CHRONICLER_SERIES_NAMES (they're
    repo-traffic signals, not resident-progress signals).
    """
    from agents.chronicler.scrapers import SCRAPERS

    orphans = sorted(n for n in CHRONICLER_SERIES_NAMES if n not in SCRAPERS)
    assert not orphans, (
        f"CHRONICLER_SERIES_NAMES entries with no backing scraper: {orphans}. "
        "A series name here must match a key in agents/chronicler/scrapers.py "
        "SCRAPERS, or MetricsSeriesSource will silently count zero for it."
    )


@pytest.mark.asyncio
async def test_metrics_series_source_returns_uniform_count(test_db):
    src = MetricsSeriesSource(test_db)
    uuids = [f"44444444-0000-0000-0000-{i:012d}" for i in range(3)]
    out = await src.fetch(uuids, timedelta(hours=26))
    # Chronicler source has no agent_id column; result is the same per-uuid count
    # because all uuids share the same name-filtered total.
    assert len({v for v in out.values()}) == 1
    assert set(out.keys()) == set(uuids)


@pytest.mark.asyncio
async def test_kg_source_empty_uuid_list_no_query(test_db):
    src = KnowledgeDiscoverySource(test_db)
    out = await src.fetch([], timedelta(hours=1))
    assert out == {}


# --- CheckinSource: substrate-agnostic productivity via core.agent_state ---


async def _seed_identity_with_state(conn, handle, *, synthetic):
    """Insert (or reset) the agents->identities->agent_state chain for a handle
    + one agent_state row. Idempotent across re-runs via delete-first.
    (identities.agent_id is a FK to core.agents.id, so the agent must exist.)"""
    await conn.execute(
        "DELETE FROM core.agent_state WHERE identity_id IN "
        "(SELECT identity_id FROM core.identities WHERE agent_id=$1)", handle
    )
    await conn.execute("DELETE FROM core.identities WHERE agent_id=$1", handle)
    await conn.execute("DELETE FROM core.agents WHERE id=$1", handle)
    await conn.execute(
        "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test')", handle
    )
    iid = await conn.fetchval(
        "INSERT INTO core.identities (agent_id, api_key_hash) VALUES ($1, 'test') "
        "RETURNING identity_id", handle
    )
    await conn.execute(
        "INSERT INTO core.agent_state (identity_id, synthetic) VALUES ($1, $2)",
        iid, synthetic,
    )


@pytest.mark.asyncio
async def test_checkin_source_returns_zero_for_unknown_handle(test_db):
    src = CheckinSource(test_db)
    out = await src.fetch(["no-such-handle-000"], timedelta(minutes=30))
    assert out == {"no-such-handle-000": 0}


@pytest.mark.asyncio
async def test_checkin_source_counts_real_checkins_via_agent_id_join(test_db):
    # Exercises the agent_id(handle) -> identity_id(PK) join. A naive
    # `WHERE identity_id = handle` would return 0 here (the 2026-06-03 bug).
    handle = "test-checkin-handle-real"
    async with test_db.acquire() as conn:
        await _seed_identity_with_state(conn, handle, synthetic=False)
    src = CheckinSource(test_db)
    out = await src.fetch([handle], timedelta(minutes=30))
    assert out[handle] == 1


@pytest.mark.asyncio
async def test_checkin_source_excludes_synthetic_bootstrap_rows(test_db):
    handle = "test-checkin-handle-synthetic"
    async with test_db.acquire() as conn:
        await _seed_identity_with_state(conn, handle, synthetic=True)
    src = CheckinSource(test_db)
    out = await src.fetch([handle], timedelta(minutes=30))
    assert out[handle] == 0  # genesis/bootstrap seeding is not "work done"


@pytest.mark.asyncio
async def test_checkin_source_empty_uuid_list_no_query(test_db):
    src = CheckinSource(test_db)
    out = await src.fetch([], timedelta(minutes=30))
    assert out == {}
