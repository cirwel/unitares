from unittest.mock import AsyncMock

import pytest

from src.db.mixins.graph import GraphMixin


class _GraphBackend(GraphMixin):
    def __init__(self):
        self._age_graph = "governance_graph"


@pytest.mark.asyncio
async def test_nonsuperuser_age_uses_administrator_preload_without_privileged_load():
    conn = AsyncMock()
    conn.fetchval.return_value = True
    await _GraphBackend()._prepare_age_connection(conn)
    conn.execute.assert_awaited_once_with('SET search_path = ag_catalog, core, audit, public')


@pytest.mark.asyncio
async def test_graph_available_creates_missing_graph():
    backend = _GraphBackend()
    conn = AsyncMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = conn
    acquire_ctx.__aexit__.return_value = False
    backend.acquire = lambda: acquire_ctx

    conn.fetchval.return_value = False
    conn.fetch.return_value = [{"result": "1"}]

    result = await backend.graph_available()

    assert result is True
    conn.execute.assert_any_call("LOAD 'age'")
    conn.execute.assert_any_call("SET search_path = ag_catalog, core, audit, public")
    conn.execute.assert_any_call(
        "SELECT * FROM ag_catalog.create_graph($1)",
        "governance_graph",
    )


@pytest.mark.asyncio
async def test_run_cypher_repairs_stale_graph_and_retries():
    backend = _GraphBackend()
    conn = AsyncMock()
    fresh_conn = AsyncMock()

    # Set up acquire() to return a fresh connection for DDL repair
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fresh_conn
    acquire_ctx.__aexit__.return_value = False
    backend.acquire = lambda: acquire_ctx

    conn.fetchval.return_value = True
    conn.fetch.side_effect = [
        Exception("graph with oid 17401 does not exist"),
        [{"result": '{"ok": true}::agtype'}],
    ]

    result = await backend._run_cypher_on_conn(conn, "RETURN 1", {})

    assert result == [{"ok": True}]
    # DDL should run on the fresh connection, not the transactional one
    fresh_conn.execute.assert_any_call(
        "SELECT * FROM ag_catalog.drop_graph($1, true)",
        "governance_graph",
    )
    fresh_conn.execute.assert_any_call(
        "SELECT * FROM ag_catalog.create_graph($1)",
        "governance_graph",
    )


@pytest.mark.asyncio
async def test_run_cypher_preserves_escaped_control_chars_in_replacement():
    backend = _GraphBackend()
    conn = AsyncMock()
    conn.fetchval.return_value = True

    captured_sql = {}

    async def _fetch(sql):
        captured_sql["sql"] = sql
        return [{"result": '"ok"'}]

    conn.fetch.side_effect = _fetch

    await backend._run_cypher_on_conn(conn, "RETURN ${value}", {"value": "line1\nline2"})

    assert "\\n" in captured_sql["sql"]
