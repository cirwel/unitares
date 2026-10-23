"""Tests for the backend-independent KG write budget.

The invariant these pin: the per-agent store budget is a property of the write
contract, not of whichever storage driver happens to be mounted. Before this,
the budget lived only in the AGE driver, so selecting the PostgreSQL FTS
backend silently removed the anti-poisoning limit with no error and no failing
test. `test_every_backend_charges_the_budget` is the regression guard — it is
meant to fail if a future backend forgets to charge.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.knowledge_graph import DiscoveryNode
from src.storage.knowledge_graph import KnowledgeGraphAGE
from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres
from src.storage.kg_write_budget import (
    DEFAULT_STORES_PER_HOUR,
    WriteBudgetExceeded,
    check_store_budget,
)


def make_discovery(agent_id: str = "agent-1") -> DiscoveryNode:
    return DiscoveryNode(
        id="disc-001",
        agent_id=agent_id,
        type="insight",
        summary="Test discovery",
        details="Some details",
    )


def make_conn(*fetchvals) -> MagicMock:
    """A connection whose transaction() is an async context manager."""
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=list(fetchvals))
    conn.execute = AsyncMock()
    return conn


class TestCheckStoreBudget:

    @pytest.mark.asyncio
    async def test_charges_the_postgres_ledger(self):
        conn = make_conn("agent-1")  # guarded INSERT returned a row
        await check_store_budget("agent-1", db=MagicMock(), conn=conn)

        assert "INSERT INTO audit.rate_limits" in conn.fetchval.await_args.args[0]
        assert conn.fetchval.await_args.args[3] == DEFAULT_STORES_PER_HOUR

    @pytest.mark.asyncio
    async def test_raises_when_budget_spent(self):
        conn = make_conn(None, 20)  # INSERT blocked, then the count
        with pytest.raises(WriteBudgetExceeded, match="has stored 20"):
            await check_store_budget("agent-1", db=MagicMock(), conn=conn)

    @pytest.mark.asyncio
    async def test_stays_a_valueerror_for_existing_handlers(self):
        """Callers predating this module catch ValueError; keep that working."""
        with pytest.raises(ValueError):
            await check_store_budget("agent-1", db=MagicMock(), conn=make_conn(None, 20))

    @pytest.mark.asyncio
    async def test_a_redis_allow_does_not_skip_the_ledger(self):
        """Regression: a Redis fast path answered "allowed" (always, with no
        Redis) and returned before the PostgreSQL ledger, so the budget was not
        enforced. Even a limiter that admits everything must not bypass it."""
        admit_all = MagicMock()
        admit_all.check = AsyncMock(return_value=True)
        admit_all.record = AsyncMock()
        admit_all.get_count = AsyncMock(return_value=0)
        conn = make_conn(None, 20)  # the ledger is spent
        with patch("src.cache.get_rate_limiter", return_value=admit_all):
            with pytest.raises(WriteBudgetExceeded, match="has stored 20"):
                await check_store_budget("agent-1", db=MagicMock(), conn=conn)

    @pytest.mark.asyncio
    async def test_prune_is_bounded_and_never_waits(self):
        conn = make_conn("agent-1")
        await check_store_budget("agent-1", db=MagicMock(), conn=conn)
        prune_sql = conn.execute.await_args_list[-1].args[0]
        assert "DELETE FROM audit.rate_limits" in prune_sql
        assert "SKIP LOCKED" in prune_sql and "LIMIT $2" in prune_sql

    @pytest.mark.asyncio
    async def test_serializes_the_agent_before_counting(self):
        conn = make_conn("agent-1")
        await check_store_budget("agent-1", db=MagicMock(), conn=conn)

        conn.transaction.assert_called_once()
        lock_sql, lock_key = conn.execute.await_args_list[0].args
        assert "pg_advisory_xact_lock" in lock_sql and lock_key == "kg-store-budget:agent-1"

    @pytest.mark.asyncio
    async def test_uses_the_callers_connection_when_given(self):
        db = MagicMock()
        await check_store_budget("agent-1", db=db, conn=make_conn("agent-1"))
        db.acquire.assert_not_called()


class TestPostgresBudgetLive:
    """Against a real database: a store racing an uncommitted one may not overshoot."""

    @pytest.mark.asyncio
    async def test_store_racing_an_uncommitted_charge_is_refused(self, live_postgres_backend):
        """Deterministic interleaving of the race the advisory lock closes.

        A charges the 20th unit inside a transaction it holds open. Under READ
        COMMITTED, B's guarded INSERT cannot see A's uncommitted row, so without
        the lock B counts 19 and is admitted too (21 in the hour). With it, B
        blocks on the lock (observed in pg_locks, not assumed from a sleep),
        then counts 20 after A commits and is refused.
        """
        import asyncio
        import uuid as _uuid

        agent = f"budget-race-{_uuid.uuid4().hex[:12]}"
        db = live_postgres_backend

        async def advisory_waiters() -> int:
            """Waiters on THIS agent's budget lock: a bigint advisory key is
            split into classid (high 32 bits) and objid (low 32 bits)."""
            async with db.acquire() as probe:
                return await probe.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                    "AND classid::bigint = (hashtextextended($1, 0) >> 32) & 4294967295 "
                    "AND objid::bigint = hashtextextended($1, 0) & 4294967295",
                    f"kg-store-budget:{agent}",
                )

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO audit.rate_limits (agent_id, timestamp) "
                    "SELECT $1, clock_timestamp() - make_interval(secs => g) "
                    "FROM generate_series(1, $2) AS g",
                    agent, DEFAULT_STORES_PER_HOUR - 1,
                )
            async with db.acquire() as conn_a:
                async with conn_a.transaction():
                    await check_store_budget(agent, db=db, conn=conn_a)  # the 20th
                    racer = asyncio.ensure_future(check_store_budget(agent, db=db))
                    for _ in range(100):
                        if racer.done() or await advisory_waiters():
                            break
                        await asyncio.sleep(0.05)
                    assert not racer.done(), "the racing store ran while the 20th was uncommitted"
                    assert await advisory_waiters() >= 1, "the racing store never waited on the lock"
            with pytest.raises(WriteBudgetExceeded):
                await asyncio.wait_for(racer, timeout=10)
        finally:
            async with db.acquire() as conn:
                await conn.execute("DELETE FROM audit.rate_limits WHERE agent_id = $1", agent)

    @pytest.mark.asyncio
    async def test_future_dated_rows_do_not_count(self, live_postgres_backend):
        """An older version wrote the application host's naive clock; on a host
        ahead of the database those rows sit in the future and must not lock the
        agent out until the database clock catches up."""
        import uuid as _uuid

        agent = f"budget-skew-{_uuid.uuid4().hex[:12]}"
        db = live_postgres_backend
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO audit.rate_limits (agent_id, timestamp) "
                    "SELECT $1, clock_timestamp() + make_interval(mins => g) "
                    "FROM generate_series(1, $2) AS g",
                    agent, DEFAULT_STORES_PER_HOUR,
                )
            await check_store_budget(agent, db=db)  # admitted despite 20 future rows
        finally:
            async with db.acquire() as conn:
                await conn.execute("DELETE FROM audit.rate_limits WHERE agent_id = $1", agent)


class TestPostgresBackendChargesBudget:
    """The gap this change closes: the FTS backend previously had no budget."""

    @pytest.mark.asyncio
    async def test_add_discovery_charges_the_budget(self):
        kg = KnowledgeGraphPostgres()
        mock_db = MagicMock()
        mock_db.kg_add_discovery = AsyncMock()
        kg._get_db = AsyncMock(return_value=mock_db)

        with patch(
            "src.storage.knowledge_graph_postgres.check_store_budget",
            new=AsyncMock(),
        ) as charged:
            await kg.add_discovery(make_discovery())

        charged.assert_awaited_once()
        assert charged.await_args.args[0] == "agent-1"
        assert charged.await_args.kwargs["limit"] == DEFAULT_STORES_PER_HOUR
        mock_db.kg_add_discovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_is_refused_when_budget_spent(self):
        kg = KnowledgeGraphPostgres()
        mock_db = MagicMock()
        mock_db.kg_add_discovery = AsyncMock()
        kg._get_db = AsyncMock(return_value=mock_db)

        with patch(
            "src.storage.knowledge_graph_postgres.check_store_budget",
            new=AsyncMock(side_effect=WriteBudgetExceeded("spent")),
        ):
            with pytest.raises(WriteBudgetExceeded):
                await kg.add_discovery(make_discovery())

        mock_db.kg_add_discovery.assert_not_awaited()


class TestBudgetIsBackendIndependent:

    def test_every_backend_charges_the_budget(self):
        """A backend that stores discoveries must declare a store budget.

        Guards the original failure mode: the limit existed in one driver, the
        canonical driver had none, and nothing caught it. If you add a backend,
        this test is where you find out you skipped the budget.
        """
        backends = [KnowledgeGraphAGE, KnowledgeGraphPostgres]

        for backend in backends:
            assert hasattr(backend, "add_discovery"), backend.__name__
            instance = backend()
            assert getattr(instance, "rate_limit_stores_per_hour", None) == (
                DEFAULT_STORES_PER_HOUR
            ), f"{backend.__name__} does not declare a store budget"

    @pytest.mark.asyncio
    async def test_age_backend_delegates_to_shared_budget(self):
        kg = KnowledgeGraphAGE()
        kg._get_db = AsyncMock(return_value=MagicMock())

        with patch(
            "src.storage.kg_write_budget.check_store_budget", new=AsyncMock()
        ) as charged:
            await kg._check_rate_limit("agent-1", conn=MagicMock())

        charged.assert_awaited_once()
        assert charged.await_args.args[0] == "agent-1"
        assert charged.await_args.kwargs["limit"] == DEFAULT_STORES_PER_HOUR
