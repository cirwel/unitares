"""closure_class and closure_evidence are written, cleared and read back.

Migration 064 added the two columns in 2026-08 and the handler has validated
both since, but until 2026-09-26 no storage path wrote them: the AGE SQL sync,
the AGE SQL-only fallback and the Postgres backend each copied a fixed column
set that left them out, and every row read NULL. These tests hold each storage
path to what it writes, with fakes that capture the SQL and Cypher together
with their parameters, and each read path to what it returns.

The live-database half (the constraint migration 071 widens, and the
lifecycle's archive and cold moves for a classified row) is in
tests/test_migration_071_closure_class_tiering.py.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.knowledge_graph import (
    CLOSURE_CLASS_ADMITTING_STATUSES,
    CLOSURE_CLASS_CLEARING_STATUSES,
    VALID_DISCOVERY_STATUSES,
    DiscoveryNode,
    apply_closure_reopen_rule,
    closure_evidence_from_stored,
)
from src.mcp_handlers.knowledge import handlers as kg_handlers
from tests.helpers import parse_result


EVIDENCE = {
    "deployed": "abc123 is an ancestor of the running build_sha",
    "observed": "the new response field appeared on a live read",
}


# ---------------------------------------------------------------------------
# Fakes that capture every statement with its parameters
# ---------------------------------------------------------------------------


class _Capture:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []
        self.cypher: list[tuple[str, dict]] = []


class _Conn:
    def __init__(self, capture: _Capture):
        self._capture = capture

    async def execute(self, sql, *params):
        self._capture.sql.append((sql, params))
        return "UPDATE 1"

    async def executemany(self, sql, rows):
        self._capture.sql.append((sql, tuple(rows)))

    async def fetchval(self, sql, *params):
        self._capture.sql.append((sql, params))
        return "d-1"


class _Pool:
    """The ExecutorPool's surface (#218): acquire() and no query methods, so
    a storage path that queries the pool directly fails here as it does live."""

    def __init__(self, capture: _Capture):
        self._capture = capture

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._capture)


class _Db:
    def __init__(self, capture: _Capture, *, graph_ok: bool = True):
        self._capture = capture
        self._pool = _Pool(capture)
        self._graph_ok = graph_ok

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._capture)

    @asynccontextmanager
    async def transaction(self):
        yield _Conn(self._capture)

    async def graph_available(self):
        return self._graph_ok

    async def graph_query(self, cypher, params, conn=None):
        self._capture.cypher.append((cypher, dict(params or {})))
        return [{"id": "d-1"}]


async def _run_update_path(path: str, updates: dict) -> _Capture:
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    capture = _Capture()
    if path == "age":
        backend = KnowledgeGraphAGE()
        backend._db = _Db(capture)
        backend._refresh_embedding = AsyncMock()
        assert await backend.update_discovery("d-1", updates)
    elif path == "age_sql_fallback":
        backend = KnowledgeGraphAGE()
        backend._db = _Db(capture, graph_ok=False)
        assert await backend.update_discovery("d-1", updates)
    else:
        backend = KnowledgeGraphPostgres()
        backend._db = _Db(capture)
        assert await backend.update_discovery("d-1", updates)
    return capture


def _update_values(capture: _Capture) -> dict:
    """column -> value the one UPDATE of knowledge.discoveries assigns."""
    updates = [
        (sql, params)
        for sql, params in capture.sql
        if "UPDATE knowledge.discoveries" in sql
    ]
    assert len(updates) == 1, [sql for sql, _ in capture.sql]
    sql, params = updates[0]
    assigned = {}
    for column, index in re.findall(r"(\w+)\s*=\s*\$(\d+)", sql.split("WHERE")[0]):
        assigned[column] = params[int(index) - 1]
    return assigned


def _cypher_values(capture: _Capture) -> dict:
    """node property -> value the AGE SET assigns."""
    sets = [
        (cypher, params) for cypher, params in capture.cypher if "SET" in cypher
    ]
    assert len(sets) == 1, capture.cypher
    cypher, params = sets[0]
    return {
        prop: params[name]
        for prop, name in re.findall(r"d\.(\w+)\s*=\s*\$\{(\w+)\}", cypher)
    }


UPDATE_PATHS = ("age", "age_sql_fallback", "postgres")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", UPDATE_PATHS)
async def test_every_update_path_writes_the_class_and_its_evidence(path):
    capture = await _run_update_path(
        path,
        {
            "status": "resolved",
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        },
    )
    written = _update_values(capture)
    assert written["status"] == "resolved"
    assert written["closure_class"] == "fix_verified"
    # jsonb without a codec takes JSON text; it must decode to the object.
    assert json.loads(written["closure_evidence"]) == EVIDENCE


@pytest.mark.asyncio
async def test_the_age_node_carries_the_class_too():
    """get_discovery and full-text hydration read the AGE node, not the row."""
    capture = await _run_update_path(
        "age",
        {
            "status": "resolved",
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        },
    )
    node = _cypher_values(capture)
    assert node["closure_class"] == "fix_verified"
    assert json.loads(node["closure_evidence"]) == EVIDENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("path", UPDATE_PATHS)
@pytest.mark.parametrize("status", sorted(CLOSURE_CLASS_CLEARING_STATUSES))
async def test_a_reopening_update_clears_the_pair_on_every_path(path, status):
    """A writer that knows nothing about classes cannot leave one on an open row.

    dialectic's resolution sets status='open' with details and updated_at only;
    the constraint refuses a class on an open row, so storage clears the pair.
    """
    capture = await _run_update_path(
        path, {"status": status, "details": "reopened", "updated_at": "2026-09-26T00:00:00+00:00"}
    )
    written = _update_values(capture)
    assert written["status"] == status
    assert "closure_class" in written and written["closure_class"] is None
    assert "closure_evidence" in written and written["closure_evidence"] is None
    if path == "age":
        node = _cypher_values(capture)
        assert node["closure_class"] is None
        assert node["closure_evidence"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", UPDATE_PATHS)
async def test_an_update_that_leaves_status_alone_leaves_the_class_alone(path):
    capture = await _run_update_path(path, {"details": "more", "updated_at": "2026-09-26T00:00:00+00:00"})
    written = _update_values(capture)
    assert "closure_class" not in written
    assert "closure_evidence" not in written


@pytest.mark.parametrize("status", sorted(CLOSURE_CLASS_ADMITTING_STATUSES))
def test_the_reopen_rule_leaves_every_admitting_status_alone(status):
    updates = {"status": status, "closure_class": "duplicate"}
    assert apply_closure_reopen_rule(updates) is updates


def test_admitting_and_clearing_statuses_partition_the_vocabulary():
    assert CLOSURE_CLASS_ADMITTING_STATUSES | CLOSURE_CLASS_CLEARING_STATUSES == VALID_DISCOVERY_STATUSES
    assert not CLOSURE_CLASS_ADMITTING_STATUSES & CLOSURE_CLASS_CLEARING_STATUSES


def test_a_created_age_node_carries_the_pair_only_when_classified():
    from src.db.age_queries import create_discovery_node

    _cypher, bare = create_discovery_node(
        discovery_id="d-1", agent_id="a-1", discovery_type="note", summary="s"
    )
    assert "closure_class" not in bare and "closure_evidence" not in bare
    _cypher, classified = create_discovery_node(
        discovery_id="d-1", agent_id="a-1", discovery_type="note", summary="s",
        status="resolved", closure_class="duplicate",
        closure_evidence=json.dumps({"of": "d-0"}),
    )
    assert classified["closure_class"] == "duplicate"
    assert json.loads(classified["closure_evidence"]) == {"of": "d-0"}


@pytest.mark.asyncio
async def test_rehydrating_an_age_node_from_its_row_keeps_the_pair():
    """Rows written without an AGE node are rebuilt from SQL, class included."""
    from datetime import datetime, timezone
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    capture = _Capture()
    backend = KnowledgeGraphAGE()
    backend._db = _Db(capture)
    row = {
        "id": "d-1", "agent_id": "a-1", "type": "note", "summary": "s",
        "status": "archived", "created_at": datetime.now(timezone.utc),
        "closure_class": "obsolete", "closure_evidence": json.dumps({"gone": "x"}),
    }
    await backend._import_discovery_row(_Conn(capture), row)
    node_create = next(params for cypher, params in capture.cypher if "MERGE (d:Discovery" in cypher)
    assert node_create["closure_class"] == "obsolete"
    assert json.loads(node_create["closure_evidence"]) == {"gone": "x"}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_every_converter_returns_the_pair():
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    stored = json.dumps(EVIDENCE)
    row = {
        "id": "d-1", "agent_id": "a-1", "type": "note", "summary": "s",
        "status": "archived", "closure_class": "fix_verified", "closure_evidence": stored,
    }
    for node in (
        KnowledgeGraphAGE()._dict_to_discovery(row),
        KnowledgeGraphAGE()._node_to_discovery(row),
        KnowledgeGraphPostgres()._dict_to_discovery(row),
        DiscoveryNode.from_dict(row),
    ):
        assert node.closure_class == "fix_verified"
        assert node.closure_evidence == EVIDENCE


def test_the_row_parser_decodes_jsonb_text():
    from src.db.mixins.knowledge_graph import KnowledgeGraphMixin

    parsed = KnowledgeGraphMixin()._row_to_discovery_dict(
        {"id": "d-1", "closure_class": "duplicate", "closure_evidence": '{"of": "d-0"}'}
    )
    assert parsed["closure_evidence"] == {"of": "d-0"}


def test_unreadable_stored_evidence_reads_as_none():
    assert closure_evidence_from_stored("not json") is None
    assert closure_evidence_from_stored('["a list"]') is None


def test_to_dict_names_the_class_only_on_a_classified_row():
    """An unclassified read is byte-identical to one from before the class was stored."""
    bare = DiscoveryNode(id="d-1", agent_id="a-1", type="note", summary="s")
    assert "closure_class" not in bare.to_dict()
    assert "closure_evidence" not in bare.to_dict()

    classified = DiscoveryNode(
        id="d-1", agent_id="a-1", type="note", summary="s", status="resolved",
        closure_class="fix_verified", closure_evidence=EVIDENCE,
    )
    full = classified.to_dict(include_details=True)
    summary = classified.to_dict(include_details=False)
    assert full["closure_class"] == summary["closure_class"] == "fix_verified"
    assert full["closure_evidence"] == EVIDENCE
    assert "closure_evidence" not in summary


def _search_state(document):
    return SimpleNamespace(
        results=[document],
        request=SimpleNamespace(include_provenance=False),
    )


@pytest.mark.parametrize("include_details", [False, True])
def test_search_results_carry_the_class_and_expanded_ones_the_evidence(include_details):
    document = DiscoveryNode(
        id="d-1", agent_id="a-1", type="bug_found", summary="s", details="body",
        status="resolved", closure_class="fix_verified", closure_evidence=EVIDENCE,
        provenance={"writer_label_at_write": "writer"},
    )
    [item] = kg_handlers._serialize_search_discoveries(
        _search_state(document), include_details=include_details
    )
    assert item["closure_class"] == "fix_verified"
    if include_details:
        assert item["closure_evidence"] == EVIDENCE
    else:
        assert "closure_evidence" not in item


def test_the_lean_search_digest_is_unchanged():
    """Left alone on purpose: the lean digest keeps its field budget."""
    assert "closure_class" not in kg_handlers._LEAN_DISCOVERY_FIELDS


@pytest.fixture
def details_env():
    server = MagicMock()
    server.agent_metadata = {}
    graph = AsyncMock()
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            new_callable=AsyncMock,
            return_value=graph,
        ),
        patch(
            "src.mcp_handlers.knowledge.handlers._broadcast_knowledge_read",
            new_callable=AsyncMock,
        ),
    ):
        yield graph


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [None, 3], ids=["whole", "paginated"])
async def test_the_details_route_returns_the_class_and_its_evidence(details_env, length):
    details_env.get_discovery = AsyncMock(
        return_value=DiscoveryNode(
            id="d-1", agent_id="a-1", type="bug_found", summary="s",
            details="a long body", status="archived",
            closure_class="fix_verified", closure_evidence=EVIDENCE,
        )
    )
    arguments = {"discovery_id": "d-1"}
    if length is not None:
        arguments["length"] = length
    data = parse_result(await kg_handlers.handle_get_discovery_details(arguments))
    assert data["discovery"]["closure_class"] == "fix_verified"
    assert data["discovery"]["closure_evidence"] == EVIDENCE
    if length is not None:
        assert data["pagination"]["has_more"] is True, "premise: the page is sliced"
