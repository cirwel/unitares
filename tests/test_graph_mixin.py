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
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=False)
    conn.fetch.side_effect = [
        Exception("graph with oid 17401 does not exist"),
        [{"result": '{"ok": true}::agtype'}],
    ]

    result = await backend._run_cypher_on_conn(conn, "RETURN 1", {})

    assert result == [{"ok": True}]
    fresh_conn.execute.assert_any_call("LOAD 'age'")
    fresh_conn.execute.assert_any_call("SET search_path = ag_catalog, core, audit, public")
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
async def test_run_cypher_reconciles_restored_graph_oid_without_dropping_data():
    backend = _GraphBackend()
    conn = AsyncMock()
    fresh_conn = AsyncMock()

    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fresh_conn
    acquire_ctx.__aexit__.return_value = False
    backend.acquire = lambda: acquire_ctx

    conn.fetchval.return_value = True
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=True, reconcile=True)
    fresh_conn.fetch.return_value = [{"result": "1"}]
    conn.fetch.side_effect = [
        Exception("graph with oid 30252 does not exist"),
        [{"result": '{"restored": true}::agtype'}],
    ]

    result = await backend._run_cypher_on_conn(conn, "MATCH (d:Discovery) RETURN d", {})

    assert result == [{"restored": True}]
    fresh_conn.execute.assert_any_call("LOAD 'age'")
    fresh_conn.execute.assert_any_call("SET search_path = ag_catalog, core, audit, public")
    executed_sqls = [call.args[0] for call in fresh_conn.execute.await_args_list]
    assert not any("drop_graph" in sql for sql in executed_sqls)
    assert not any("create_graph" in sql for sql in executed_sqls)
    assert backend._graph_exists_confirmed is True


@pytest.mark.asyncio
async def test_graph_available_reconciles_stale_oid_on_first_probe():
    backend = _GraphBackend()
    conn = AsyncMock()
    fresh_conn = AsyncMock()

    conns = iter([conn, fresh_conn])

    class _AcquireCtx:
        async def __aenter__(self):
            return next(conns)

        async def __aexit__(self, *args):
            return False

    backend.acquire = lambda: _AcquireCtx()

    conn.fetchval.side_effect = [False, True, False]
    conn.fetch.side_effect = [
        Exception("graph with oid 30252 does not exist"),
        [{"result": "1"}],
    ]
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=True, reconcile=True)
    fresh_conn.fetch.return_value = [{"result": "1"}]

    assert await backend.graph_available() is True
    executed_sqls = [call.args[0] for call in fresh_conn.execute.await_args_list]
    assert not any("drop_graph" in sql for sql in executed_sqls)


STALE_ERROR = "graph with oid 30252 does not exist"


def _catalog_fetchval(*, schema_exists, reconcile=False):
    """Answer fetchval by the SQL asked, so a test cannot pass on call order alone."""

    async def fetchval(sql, *args):
        if "current_setting('is_superuser')" in sql:
            return False
        if "UPDATE ag_catalog.ag_graph" in sql:
            if isinstance(reconcile, Exception):
                raise reconcile
            return reconcile
        if "FROM pg_namespace WHERE nspname" in sql:
            return schema_exists
        raise AssertionError(f"unexpected SQL: {sql}")

    return fetchval


def _backend_with_fresh_conn():
    backend = _GraphBackend()
    fresh_conn = AsyncMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fresh_conn
    acquire_ctx.__aexit__.return_value = False
    backend.acquire = lambda: acquire_ctx
    return backend, fresh_conn


def _assert_graph_never_recreated(fresh_conn):
    executed_sqls = [call.args[0] for call in fresh_conn.execute.await_args_list]
    assert not any("drop_graph" in sql or "create_graph" in sql for sql in executed_sqls)


@pytest.mark.asyncio
async def test_repair_keeps_graph_already_aligned_by_a_concurrent_repair():
    backend, fresh_conn = _backend_with_fresh_conn()
    conn = AsyncMock()
    conn.fetchval.return_value = True
    # The schema exists, and the reconcile finds nothing left to update.
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=True, reconcile=False)
    fresh_conn.fetch.return_value = [{"result": "1"}]
    conn.fetch.side_effect = [Exception(STALE_ERROR), [{"result": '"ok"'}]]

    assert await backend._run_cypher_on_conn(conn, "RETURN 1", {}) == ["ok"]
    _assert_graph_never_recreated(fresh_conn)
    assert backend._graph_exists_confirmed is True


@pytest.mark.asyncio
async def test_repair_keeps_graph_when_role_cannot_update_catalog():
    backend, fresh_conn = _backend_with_fresh_conn()
    conn = AsyncMock()
    conn.fetchval.return_value = True
    fresh_conn.fetchval.side_effect = _catalog_fetchval(
        schema_exists=True,
        reconcile=Exception("permission denied for table ag_graph"),
    )
    conn.fetch.side_effect = Exception(STALE_ERROR)

    with pytest.raises(Exception, match="graph with oid"):
        await backend._run_cypher_on_conn(conn, "RETURN 1", {})
    _assert_graph_never_recreated(fresh_conn)


@pytest.mark.asyncio
async def test_repair_keeps_graph_when_probe_fails_after_reconcile():
    backend, fresh_conn = _backend_with_fresh_conn()
    conn = AsyncMock()
    conn.fetchval.return_value = True
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=True, reconcile=True)
    fresh_conn.fetch.side_effect = Exception("canceling statement due to statement timeout")
    conn.fetch.side_effect = Exception(STALE_ERROR)

    with pytest.raises(Exception, match="graph with oid"):
        await backend._run_cypher_on_conn(conn, "RETURN 1", {})
    _assert_graph_never_recreated(fresh_conn)


@pytest.mark.asyncio
async def test_availability_probe_never_recreates_a_graph_with_no_schema():
    backend = _GraphBackend()
    conn = AsyncMock()
    fresh_conn = AsyncMock()
    conns = iter([conn, fresh_conn])

    class _AcquireCtx:
        async def __aenter__(self):
            return next(conns)

        async def __aexit__(self, *args):
            return False

    backend.acquire = lambda: _AcquireCtx()
    conn.fetchval.side_effect = [False, True]
    conn.fetch.side_effect = Exception(STALE_ERROR)
    fresh_conn.fetchval.side_effect = _catalog_fetchval(schema_exists=False)

    assert await backend.graph_available() is False
    _assert_graph_never_recreated(fresh_conn)


def test_reconcile_sql_remaps_the_graph_row_and_its_labels():
    sql = " ".join(GraphMixin._RECONCILE_AGE_GRAPH_OID_SQL.split())
    assert "UPDATE ag_catalog.ag_graph AS g SET graphid = old_graph.new_id" in sql
    assert "UPDATE ag_catalog.ag_label AS l SET graph = old_graph.new_id" in sql
    assert "WHERE l.graph = old_graph.old_id" in sql
    assert "g.graphid <> n.oid" in sql


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
