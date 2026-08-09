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


def make_limiter(allowed: bool, count: int = 99) -> MagicMock:
    limiter = MagicMock()
    limiter.check = AsyncMock(return_value=allowed)
    limiter.get_count = AsyncMock(return_value=count)
    limiter.record = AsyncMock()
    return limiter


class TestCheckStoreBudget:

    @pytest.mark.asyncio
    async def test_records_the_write_when_under_budget(self):
        limiter = make_limiter(allowed=True)
        with patch("src.cache.get_rate_limiter", return_value=limiter):
            await check_store_budget("agent-1", db=MagicMock())

        limiter.record.assert_awaited_once()
        assert limiter.check.await_args.kwargs["limit"] == DEFAULT_STORES_PER_HOUR
        assert limiter.check.await_args.kwargs["operation"] == "kg_store"

    @pytest.mark.asyncio
    async def test_raises_when_budget_spent(self):
        limiter = make_limiter(allowed=False, count=20)
        with patch("src.cache.get_rate_limiter", return_value=limiter):
            with pytest.raises(WriteBudgetExceeded, match="has stored 20"):
                await check_store_budget("agent-1", db=MagicMock())

        limiter.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stays_a_valueerror_for_existing_handlers(self):
        """Callers predating this module catch ValueError; keep that working."""
        limiter = make_limiter(allowed=False)
        with patch("src.cache.get_rate_limiter", return_value=limiter):
            with pytest.raises(ValueError):
                await check_store_budget("agent-1", db=MagicMock())

    @pytest.mark.asyncio
    async def test_falls_back_to_postgres_when_redis_unavailable(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="agent-1")  # insert succeeded
        conn.execute = AsyncMock()

        with patch("src.cache.get_rate_limiter", side_effect=RuntimeError("no redis")):
            await check_store_budget("agent-1", db=MagicMock(), conn=conn)

        assert conn.fetchval.await_count == 1
        assert "audit.rate_limits" in conn.fetchval.await_args.args[0]

    @pytest.mark.asyncio
    async def test_postgres_fallback_raises_when_insert_blocked(self):
        conn = MagicMock()
        # First fetchval = the guarded INSERT (None -> blocked), second = the count.
        conn.fetchval = AsyncMock(side_effect=[None, 20])
        conn.execute = AsyncMock()

        with patch("src.cache.get_rate_limiter", side_effect=RuntimeError("no redis")):
            with pytest.raises(WriteBudgetExceeded, match="has stored 20"):
                await check_store_budget("agent-1", db=MagicMock(), conn=conn)


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
