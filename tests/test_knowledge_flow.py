"""Cross-agent knowledge flow (/v1/knowledge/flow, Discoveries panel).

query_cross_agent_knowledge_flow had no caller, and as written it could not
run: graph_query maps one return expression to its single result column, and
the builder returned three bare columns (an AGE column-arity error). These
tests pin the builder's runnable shape, the label resolution, and the route's
gate and graph-unavailable answer.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src import knowledge_flow as kf
from src.db.age_queries import query_cross_agent_knowledge_flow
from src.http_routes import overview


# --- builder ------------------------------------------------------------------

def test_builder_returns_one_map_expression():
    cypher, params = query_cross_agent_knowledge_flow(limit=10)
    assert params == {}
    returns = [line.strip() for line in cypher.splitlines() if line.strip().startswith("RETURN")]
    assert returns == ["RETURN {builder: builder, source: source, links: links}"]
    assert "LIMIT 10" in cypher


def test_builder_follows_related_to_from_the_citing_discovery():
    cypher, _ = query_cross_agent_knowledge_flow()
    # Directed: the store path writes RELATED_TO from the new discovery to the
    # ones it cites. An undirected match would count every pair both ways.
    assert "(d1:Discovery)-[:RELATED_TO]->(d2:Discovery)" in cypher


@pytest.mark.parametrize("raw, expected", [(0, 1), (-5, 1), (7, 7)])
def test_builder_limit_is_at_least_one(raw, expected):
    cypher, _ = query_cross_agent_knowledge_flow(limit=raw)
    assert f"LIMIT {expected}" in cypher


@pytest.mark.parametrize("raw, expected", [(None, 25), ("x", 25), ("0", 1), ("500", 100), ("40", 40)])
def test_route_limit_is_clamped(raw, expected):
    assert kf.clamp_limit(raw) == expected


# --- query --------------------------------------------------------------------

class _Conn:
    def __init__(self, labels):
        self.labels, self.fetched = labels, []

    async def fetch(self, sql, ids):
        self.fetched.append(list(ids))
        return [{"id": i, "label": self.labels.get(i)} for i in ids]


class _DB:
    def __init__(self, rows, labels=None, graph="available"):
        self.rows, self.graph, self.conn = rows, graph, _Conn(labels or {})
        self.queries = []

    async def graph_status(self):
        return self.graph

    async def graph_query(self, cypher, params):
        self.queries.append(cypher)
        return self.rows

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_flows_carry_labels_and_fall_back_to_ids():
    db = _DB(
        [{"builder": "b-1", "source": "s-1", "links": 4}, {"builder": "b-2", "source": "s-1", "links": 1}],
        labels={"b-1": "builder-one", "s-1": "source-one"},
    )
    out = await kf.query_knowledge_flow(db, limit=5)
    assert out["success"] and out["graph_available"] and out["limit"] == 5
    assert out["flows"][0] == {
        "builder": "b-1", "source": "s-1", "links": 4,
        "builder_label": "builder-one", "source_label": "source-one",
    }
    assert out["flows"][1]["builder_label"] is None
    assert db.conn.fetched == [["b-1", "b-2", "s-1"]]
    assert "LIMIT 5" in db.queries[0]


@pytest.mark.asyncio
async def test_malformed_rows_are_skipped():
    db = _DB([{"builder": "b-1"}, "not-a-row", {"builder": "b-1", "source": "s-1", "links": None}])
    out = await kf.query_knowledge_flow(db)
    assert out["flows"] == [{"builder": "b-1", "source": "s-1", "links": 0, "builder_label": None, "source_label": None}]


@pytest.mark.asyncio
async def test_no_graph_is_reported_not_an_empty_graph():
    db = _DB([], graph="absent")
    out = await kf.query_knowledge_flow(db)
    assert out == {"success": True, "graph_available": False, "limit": 25, "flows": []}
    assert db.queries == []


@pytest.mark.asyncio
async def test_a_failed_graph_probe_raises_not_reports_no_graph():
    db = _DB([], graph="error")
    with pytest.raises(kf.GraphProbeError):
        await kf.query_knowledge_flow(db)
    assert db.queries == []


# --- graph_status: absent vs probe failure -------------------------------------
#
# graph_available() folds every failure into False. The panel must not tell a
# reader "no graph backend" when the backend exists and its probe failed, so
# graph_status keeps the two apart. These run the real GraphMixin probe chain
# against a fake connection.

class _AgeConn:
    def __init__(self, *, installed, probe_error=None, ext_error=None):
        self.installed, self.probe_error, self.ext_error = installed, probe_error, ext_error

    async def fetchval(self, sql, *args):
        if "pg_extension" in sql:
            if self.ext_error:
                raise self.ext_error
            return self.installed
        if "ag_graph" in sql:
            return True
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        if not self.installed and "LOAD 'age'" in sql:
            raise RuntimeError("could not access file \"age\"")
        return None

    async def fetch(self, sql, *args):
        if "cypher(" in sql:
            if self.probe_error:
                raise self.probe_error
            return [{"result": "1"}]
        return [{"id": "b-1", "label": None}]


def _mixin_db(**conn_kwargs):
    from src.db.mixins.graph import GraphMixin

    conn = _AgeConn(**conn_kwargs)

    class _Backend(GraphMixin):
        _age_graph = "governance_graph"

        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *a):
                    return False
            return _Ctx()

        async def graph_query(self, cypher, params):
            return []

    return _Backend()


@pytest.mark.asyncio
async def test_graph_status_absent_when_age_is_not_installed():
    assert await _mixin_db(installed=False).graph_status() == "absent"


@pytest.mark.asyncio
async def test_graph_status_error_when_the_probe_fails_on_an_installed_graph():
    db = _mixin_db(installed=True, probe_error=RuntimeError("connection reset"))
    assert await db.graph_available() is False  # the boolean cannot tell them apart
    assert await db.graph_status() == "error"


@pytest.mark.asyncio
async def test_graph_status_error_when_the_extension_check_fails():
    db = _mixin_db(installed=True, ext_error=RuntimeError("timeout"))
    assert await db.graph_status() == "error"


@pytest.mark.asyncio
async def test_graph_status_available_when_the_probe_succeeds():
    assert await _mixin_db(installed=True).graph_status() == "available"


@pytest.mark.asyncio
async def test_base_backend_without_graph_support_is_absent():
    from src.db.base import DatabaseBackend

    class _NoGraph:
        graph_available = DatabaseBackend.graph_available
        graph_status = DatabaseBackend.graph_status

    assert await _NoGraph().graph_status() == "absent"


@pytest.mark.asyncio
async def test_no_links_skips_the_label_lookup():
    db = _DB([])
    out = await kf.query_knowledge_flow(db)
    assert out["graph_available"] and out["flows"] == []
    assert db.conn.fetched == []


# --- route --------------------------------------------------------------------

@pytest.fixture
def _local_posture(monkeypatch):
    for key in ("UNITARES_HTTP_API_TOKEN", "UNITARES_MCP_BEARER_TOKENS", "UNITARES_REST_STRICT"):
        monkeypatch.delenv(key, raising=False)


def _client(peer):
    app = Starlette(routes=[Route("/v1/knowledge/flow", overview.http_knowledge_flow, methods=["GET"])])
    return TestClient(app, client=peer)


def test_route_is_gated(_local_posture):
    r = _client(("203.0.113.7", 44444)).get("/v1/knowledge/flow")
    assert r.status_code == 401
    assert "flows" not in r.text


def test_route_serves_flows(_local_posture, monkeypatch):
    db = _DB([{"builder": "b-1", "source": "s-1", "links": 3}], labels={"b-1": "b"})
    monkeypatch.setattr("src.db.get_db", lambda: db)
    body = _client(("127.0.0.1", 50000)).get("/v1/knowledge/flow?limit=500").json()
    assert body["success"] and body["limit"] == 100
    assert body["flows"][0]["links"] == 3 and body["flows"][0]["builder_label"] == "b"


def test_route_failure_is_a_500_without_detail(_local_posture, monkeypatch):
    class _Broken(_DB):
        async def graph_query(self, cypher, params):
            raise RuntimeError("secret detail")

    monkeypatch.setattr("src.db.get_db", lambda: _Broken([]))
    r = _client(("127.0.0.1", 50000)).get("/v1/knowledge/flow")
    assert r.status_code == 500
    assert "secret detail" not in r.text


def test_route_probe_failure_is_an_error_not_no_graph(_local_posture, monkeypatch):
    # Regression (#2946 review): a probe that raised on an installed graph used
    # to come back as 200 success with graph_available false, and the panel
    # said "No graph backend on this server".
    db = _mixin_db(installed=True, probe_error=RuntimeError("secret detail"))
    monkeypatch.setattr("src.db.get_db", lambda: db)
    r = _client(("127.0.0.1", 50000)).get("/v1/knowledge/flow")
    assert r.status_code == 503
    body = r.json()
    assert body["success"] is False
    assert "graph_available" not in body
    assert "secret detail" not in r.text


def test_route_absent_graph_stays_the_benign_empty_answer(_local_posture, monkeypatch):
    monkeypatch.setattr("src.db.get_db", lambda: _mixin_db(installed=False))
    r = _client(("127.0.0.1", 50000)).get("/v1/knowledge/flow")
    assert r.status_code == 200
    assert r.json() == {"success": True, "graph_available": False, "limit": 25, "flows": []}


def test_route_is_registered():
    from src import http_api

    import inspect

    assert http_api.http_knowledge_flow is overview.http_knowledge_flow
    assert 'Route("/v1/knowledge/flow", http_knowledge_flow, methods=["GET"])' in inspect.getsource(http_api)
