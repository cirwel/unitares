"""Factory lifetime under the per-test singleton reset added in #2101.

The unit cases run in ordinary CI. The live cases require an existing,
AGE-enabled governance_test database and explicit opt-in:

    UNITARES_TEST_AGE_SINGLETON=1 python -m pytest \
        tests/test_knowledge_graph_singleton.py -k live_age -q -ra

An explicitly requested live run fails if PostgreSQL or AGE is unavailable.
The database fixture owns its connection pool; the graph reset only drops
the factory's references. Each live case gets its own database backend and
event loop, exercising teardown/reconstruction across pytest test boundaries.
"""

import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest.mark.parametrize("backend", ["postgres", "age"])
@pytest.mark.asyncio
async def test_factory_reuses_within_test_and_starts_clean_next_test(monkeypatch, backend):
    import src.knowledge_graph as factory
    import src.storage.knowledge_graph as backends

    assert factory._graph_instance is None
    assert factory._graph_lock is None
    monkeypatch.setenv("UNITARES_KNOWLEDGE_BACKEND", backend)
    backend_type = backends.KnowledgeGraphAGE if backend == "age" else backends.KnowledgeGraphPostgres
    load = AsyncMock()
    monkeypatch.setattr(backend_type, "load", load)

    first = await factory.get_knowledge_graph()
    lock = factory._graph_lock
    assert isinstance(first, backend_type)
    assert lock is not None
    assert await factory.get_knowledge_graph() is first
    assert factory._graph_lock is lock
    load.assert_awaited_once()


@pytest_asyncio.fixture
async def live_age_backend(monkeypatch):
    if os.getenv("UNITARES_TEST_AGE_SINGLETON") != "1":
        pytest.skip("set UNITARES_TEST_AGE_SINGLETON=1 for the live AGE factory check")

    from tests.test_db_utils import (
        TEST_DB_URL,
        can_connect_to_test_db,
        ensure_test_database_schema,
    )

    assert can_connect_to_test_db(), "live AGE check requested, but governance_test is unavailable"
    await ensure_test_database_schema()
    monkeypatch.setenv("DB_POSTGRES_URL", TEST_DB_URL)
    monkeypatch.setenv("DB_POSTGRES_MIN_CONN", "1")
    monkeypatch.setenv("DB_POSTGRES_MAX_CONN", "3")
    monkeypatch.setenv("DB_AGE_GRAPH", "governance_graph")
    monkeypatch.setenv("UNITARES_KNOWLEDGE_BACKEND", "age")

    import src.db as db_module
    from src.db.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    try:
        await backend.init()
        assert await backend.graph_available(), "live AGE check requested, but AGE is unavailable"
        monkeypatch.setattr(db_module, "get_db", lambda: backend)
        yield backend
    finally:
        await backend.close()


@pytest.mark.parametrize("_cycle", ["first", "after_fixture_teardown"])
@pytest.mark.asyncio
async def test_live_age_factory_reuses_then_rebuilds(live_age_backend, _cycle):
    import src.knowledge_graph as factory
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    # The second parametrized case detects a retained instance or loop lock
    # from the first. Both cases also prove warm reuse within one test.
    assert factory._graph_instance is None
    assert factory._graph_lock is None
    graph = await factory.get_knowledge_graph()
    lock = factory._graph_lock
    assert isinstance(graph, KnowledgeGraphAGE)
    assert await graph._get_db() is live_age_backend
    assert await factory.get_knowledge_graph() is graph
    assert factory._graph_lock is lock
    assert await live_age_backend.graph_query("RETURN 1") == [1]
    assert isinstance(await graph.query(limit=1), list)
