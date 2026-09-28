"""
Tests for src/knowledge_graph_lifecycle.py - KnowledgeGraphLifecycle

Tests lifecycle policy classification, cleanup logic, and tier management.
Uses mock graph backend to avoid database dependencies.
"""

import pytest
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, List

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.knowledge_graph_lifecycle import (
    KnowledgeGraphLifecycle,
    PERMANENT_TYPES,
    PERMANENT_TAGS,
    EPHEMERAL_TAGS,
)


# --- Test fixtures ---


@dataclass
class MockDiscovery:
    """Mock discovery object for lifecycle tests."""
    id: str
    type: str
    tags: Optional[List[str]] = None
    status: str = "open"
    timestamp: Optional[str] = None
    resolved_at: Optional[str] = None
    updated_at: Optional[str] = None


def make_mock_graph(open_items=None, resolved_items=None, archived_items=None, cold_items=None):
    """Create a mock graph backend with configurable query results."""
    graph = AsyncMock()

    async def mock_query(status=None, limit=1000):
        if status == "open":
            return open_items or []
        elif status == "resolved":
            return resolved_items or []
        elif status == "archived":
            return archived_items or []
        elif status == "cold":
            return cold_items or []
        return []

    graph.query = mock_query
    graph.update_discovery = AsyncMock()
    return graph


# --- Lifecycle Policy Tests ---


class TestGetLifecyclePolicy:
    """Tests for get_lifecycle_policy()."""

    def setup_method(self):
        self.lifecycle = KnowledgeGraphLifecycle()

    def test_permanent_by_type_architectural_decision(self):
        d = MockDiscovery(id="1", type="architectural_decision")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_learning(self):
        d = MockDiscovery(id="2", type="learning")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_pattern(self):
        d = MockDiscovery(id="3", type="pattern")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_backend_only_types_are_still_permanent(self):
        """`root_cause_analysis` and `migration` match 0 of 1,784 rows because
        no MCP caller can write them — both vocabularies reject them. They are
        kept anyway: `DiscoveryNode.type` is unconstrained and the column is
        plain TEXT, so a direct backend caller can still store them, and a
        draft of this change that dropped them made an old resolved
        `root_cause_analysis` an archive candidate. Zero rows is evidence of no
        current exposure, not of impossibility."""
        for backend_only in ("root_cause_analysis", "migration"):
            d = MockDiscovery(id="4", type=backend_only)
            assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_tag(self):
        for tag in PERMANENT_TAGS:
            d = MockDiscovery(id="6", type="note", tags=[tag])
            assert self.lifecycle.get_lifecycle_policy(d) == "permanent", \
                f"Tag '{tag}' should give permanent policy"

    def test_ephemeral_by_tag(self):
        for tag in EPHEMERAL_TAGS:
            d = MockDiscovery(id="7", type="note", tags=[tag])
            assert self.lifecycle.get_lifecycle_policy(d) == "ephemeral", \
                f"Tag '{tag}' should give ephemeral policy"

    def test_standard_default(self):
        d = MockDiscovery(id="8", type="note", tags=["some-tag"])
        assert self.lifecycle.get_lifecycle_policy(d) == "standard"

    def test_standard_no_tags(self):
        d = MockDiscovery(id="9", type="bug_found", tags=None)
        assert self.lifecycle.get_lifecycle_policy(d) == "standard"

    def test_standard_empty_tags(self):
        d = MockDiscovery(id="10", type="insight", tags=[])
        assert self.lifecycle.get_lifecycle_policy(d) == "standard"

    def test_permanent_type_overrides_ephemeral_tag(self):
        """Permanent type takes priority over ephemeral tag."""
        d = MockDiscovery(id="11", type="architectural_decision", tags=["ephemeral"])
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"


# --- Cleanup Tests ---


class TestRunCleanup:
    """Tests for run_cleanup() lifecycle management."""

    @pytest.mark.asyncio
    async def test_cleanup_archives_old_ephemeral(self):
        """Old ephemeral discoveries should be archived."""
        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        d = MockDiscovery(id="eph1", type="note", tags=["ephemeral"],
                          status="open", timestamp=old_time)

        graph = make_mock_graph(open_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["ephemeral_archived"] == 1
        graph.update_discovery.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_skips_recent_ephemeral(self):
        """Recent ephemeral discoveries should not be archived."""
        recent_time = (datetime.now() - timedelta(days=1)).isoformat()
        d = MockDiscovery(id="eph2", type="note", tags=["ephemeral"],
                          status="open", timestamp=recent_time)

        graph = make_mock_graph(open_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["ephemeral_archived"] == 0
        graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_archives_old_resolved(self):
        """Resolved discoveries older than 30 days should be archived."""
        old_time = (datetime.now() - timedelta(days=45)).isoformat()
        d = MockDiscovery(id="res1", type="bug_found", tags=[],
                          status="resolved", resolved_at=old_time)

        graph = make_mock_graph(resolved_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["discoveries_archived"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_skips_permanent_resolved(self):
        """Permanent discoveries should not be archived even when old and resolved."""
        old_time = (datetime.now() - timedelta(days=45)).isoformat()
        d = MockDiscovery(id="perm1", type="architectural_decision", tags=[],
                          status="resolved", resolved_at=old_time)

        graph = make_mock_graph(resolved_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["discoveries_archived"] == 0
        assert result["skipped_permanent"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_moves_old_archived_to_cold(self):
        """Archived discoveries older than 90 days should move to cold."""
        old_time = (datetime.now() - timedelta(days=120)).isoformat()
        d = MockDiscovery(id="arch1", type="note", tags=[],
                          status="archived", updated_at=old_time)

        graph = make_mock_graph(archived_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["discoveries_to_cold"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_skips_permanent_archived_from_cold(self):
        """Permanent discoveries must not be swept to cold even if archived+old.

        Symmetry with the resolved→archived permanent-skip: 'never auto-archive'
        extends to the deeper cold tier, so a permanent entry that ended up in
        'archived' stays in default search scope instead of being buried.
        """
        old_time = (datetime.now() - timedelta(days=120)).isoformat()
        d = MockDiscovery(id="perm_arch1", type="architectural_decision", tags=[],
                          status="archived", updated_at=old_time)

        graph = make_mock_graph(archived_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["discoveries_to_cold"] == 0
        graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_never_deletes(self):
        """Cleanup should NEVER delete anything - core philosophy."""
        old_time = (datetime.now() - timedelta(days=365)).isoformat()
        d = MockDiscovery(id="old1", type="note", tags=[],
                          status="archived", updated_at=old_time)

        graph = make_mock_graph(archived_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=False)

        assert result["discoveries_deleted"] == 0

    @pytest.mark.asyncio
    async def test_dry_run_does_not_modify(self):
        """Dry run should report what would change but not modify anything."""
        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        d = MockDiscovery(id="dry1", type="note", tags=["ephemeral"],
                          status="open", timestamp=old_time)

        graph = make_mock_graph(open_items=[d])
        lifecycle = KnowledgeGraphLifecycle(graph=graph)

        result = await lifecycle.run_cleanup(dry_run=True)

        assert result["dry_run"] is True
        assert result["ephemeral_archived"] == 1
        graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_handles_errors(self):
        """Cleanup should handle errors gracefully."""
        graph = AsyncMock()
        graph.query = AsyncMock(side_effect=Exception("DB connection failed"))

        lifecycle = KnowledgeGraphLifecycle(graph=graph)
        result = await lifecycle.run_cleanup(dry_run=False)

        assert len(result["errors"]) > 0
        assert "DB connection failed" in result["errors"][0]


# --- Constants Tests ---


def test_permanent_types_are_set():
    """PERMANENT_TYPES should be a non-empty set."""
    assert isinstance(PERMANENT_TYPES, set)
    assert len(PERMANENT_TYPES) >= 3
    # Was `>= 4` and `"architecture_decision"`, which is why a set with three
    # inert entries and one misspelling satisfied it. Membership is now pinned
    # against the storable types in tests/test_lifecycle_policy_types.py.
    assert "architectural_decision" in PERMANENT_TYPES
    assert "learning" in PERMANENT_TYPES


def test_permanent_tags_are_set():
    """PERMANENT_TAGS should be a non-empty set."""
    assert isinstance(PERMANENT_TAGS, set)
    assert "permanent" in PERMANENT_TAGS
    assert "foundational" in PERMANENT_TAGS


def test_ephemeral_tags_are_set():
    """EPHEMERAL_TAGS should be a non-empty set."""
    assert isinstance(EPHEMERAL_TAGS, set)
    assert "ephemeral" in EPHEMERAL_TAGS
    assert "temp" in EPHEMERAL_TAGS
    assert "test" in EPHEMERAL_TAGS


# --- Threshold Tests ---


def test_default_thresholds():
    """Verify default lifecycle thresholds."""
    lifecycle = KnowledgeGraphLifecycle()
    assert lifecycle.RESOLVED_TO_ARCHIVED_DAYS == 30
    assert lifecycle.ARCHIVED_TO_COLD_DAYS == 90
    assert lifecycle.EPHEMERAL_ARCHIVE_DAYS == 7


# --- KG hygiene v1: superseded ⊄ lifecycle vocabulary ---


@pytest.mark.asyncio
async def test_archive_old_resolved_does_not_query_superseded():
    """v1 invariant: _archive_old_resolved queries status='resolved' only.

    Superseded entries are deliberately left hot — v1 surfaces them via the
    superseded_by field rather than archiving them. This test will fail if a
    future change broadens the lifecycle sweep to include superseded.
    """
    mock_graph = MagicMock()
    mock_graph.query = AsyncMock(return_value=[])
    mock_graph.update_discovery = AsyncMock()

    lifecycle = KnowledgeGraphLifecycle()
    lifecycle._graph = mock_graph

    outcome = await lifecycle._archive_old_resolved(datetime.now(), dry_run=False)

    # The query was called with status='resolved' — superseded is out of band
    mock_graph.query.assert_awaited_with(status="resolved", limit=1000)
    assert outcome.changed == []
    assert outcome.failed == []
    assert outcome.skipped_permanent == 0
    mock_graph.update_discovery.assert_not_awaited()


# --- The status move writes through the backend alone ---


@pytest.mark.asyncio
async def test_batch_update_status_writes_each_row_once_through_the_backend():
    """Both backends' update_discovery write knowledge.discoveries themselves
    (the AGE one syncs the row in the node's transaction), so the lifecycle
    makes no second write of the row."""
    from unittest.mock import call

    graph = make_mock_graph()
    now = datetime(2026, 9, 27, 12, 0, 0)

    moved, failed = await KnowledgeGraphLifecycle(graph=graph)._batch_update_status(
        graph, ["d-1", "d-2"], "archived", now
    )

    expected = {"status": "archived", "updated_at": now.isoformat()}
    assert graph.update_discovery.await_args_list == [
        call("d-1", expected),
        call("d-2", expected),
    ]
    assert (moved, failed) == (["d-1", "d-2"], [])


def test_every_src_import_in_the_lifecycle_module_resolves():
    """An import inside a function fails only when that function runs, and
    behind a broad except it fails silently. _batch_update_status imported
    get_postgres_backend from src.db.postgres_backend, which never existed,
    and logged the ImportError at debug level, so its "PG sync" never ran."""
    import ast
    import importlib

    import src.knowledge_graph_lifecycle as lifecycle_module

    tree = ast.parse(Path(lifecycle_module.__file__).read_text())
    unresolved = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] == "src"
        ):
            continue
        module = importlib.import_module(node.module)
        for alias in node.names:
            if hasattr(module, alias.name):
                continue
            try:
                importlib.import_module(f"{node.module}.{alias.name}")
            except ModuleNotFoundError:
                unresolved.append(f"line {node.lineno}: {node.module}.{alias.name}")
    assert unresolved == []


# --- A move the backend refuses is not counted as made ---
#
# update_discovery returns False for an update it did not make: the Postgres
# backend when no row has the id, the AGE backend when its query fails and
# rolls back. The lifecycle awaited it and dropped the result, so a refused
# move was counted, logged, and returned as done.


def make_refusing_graph(refuse=(), raise_on=(), **items):
    """make_mock_graph, but update_discovery refuses or raises for some ids."""
    graph = make_mock_graph(**items)

    async def update_discovery(discovery_id, updates):
        if discovery_id in raise_on:
            raise RuntimeError(f"connection lost while updating {discovery_id}")
        return discovery_id not in refuse

    graph.update_discovery = AsyncMock(side_effect=update_discovery)
    return graph


def _old(days):
    return (datetime.now() - timedelta(days=days)).isoformat()


@pytest.mark.asyncio
async def test_batch_update_status_returns_only_the_moves_the_backend_confirmed(caplog):
    graph = make_refusing_graph(refuse={"d-2"}, raise_on={"d-3"})
    now = datetime(2026, 9, 27, 12, 0, 0)

    with caplog.at_level("WARNING", logger="src.knowledge_graph_lifecycle"):
        moved, failed = await KnowledgeGraphLifecycle(graph=graph)._batch_update_status(
            graph, ["d-1", "d-2", "d-3", "d-4"], "archived", now
        )

    assert moved == ["d-1", "d-4"]
    assert failed == ["d-2", "d-3"]
    # A raise on d-3 did not end the batch: d-4 was still attempted.
    assert graph.update_discovery.await_count == 4
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "2 of 4 moves to archived failed" in message
    assert "d-2, d-3" in message
    assert "the backend reported no update" in message


@pytest.mark.asyncio
async def test_each_pass_returns_and_counts_only_the_moves_that_happened():
    graph = make_refusing_graph(
        refuse={"eph-bad", "res-bad", "arch-bad"},
        open_items=[
            MockDiscovery(id="eph-ok", type="note", tags=["ephemeral"], timestamp=_old(10)),
            MockDiscovery(id="eph-bad", type="note", tags=["ephemeral"], timestamp=_old(10)),
        ],
        resolved_items=[
            MockDiscovery(id="res-ok", type="bug_found", status="resolved", resolved_at=_old(45)),
            MockDiscovery(id="res-bad", type="bug_found", status="resolved", resolved_at=_old(45)),
            MockDiscovery(id="perm", type="learning", status="resolved", resolved_at=_old(45)),
        ],
        archived_items=[
            MockDiscovery(id="arch-ok", type="note", status="archived", updated_at=_old(120)),
            MockDiscovery(id="arch-bad", type="note", status="archived", updated_at=_old(120)),
        ],
    )
    lifecycle = KnowledgeGraphLifecycle(graph=graph)
    now = datetime.now()

    ephemeral = await lifecycle._archive_ephemeral(now, dry_run=False)
    resolved = await lifecycle._archive_old_resolved(now, dry_run=False)
    cold = await lifecycle._move_to_cold(now, dry_run=False)

    assert (ephemeral.changed, ephemeral.failed) == (["eph-ok"], ["eph-bad"])
    assert (resolved.changed, resolved.failed) == (["res-ok"], ["res-bad"])
    assert resolved.skipped_permanent == 1
    assert (cold.changed, cold.failed) == (["arch-ok"], ["arch-bad"])

    summary = await lifecycle.run_cleanup(dry_run=False)

    assert summary["ephemeral_archived"] == 1
    assert summary["discoveries_archived"] == 1
    assert summary["discoveries_to_cold"] == 1
    assert summary["skipped_permanent"] == 1
    assert summary["failed_updates"] == {
        "ephemeral_archived": {"count": 1, "ids": ["eph-bad"]},
        "discoveries_archived": {"count": 1, "ids": ["res-bad"]},
        "discoveries_to_cold": {"count": 1, "ids": ["arch-bad"]},
    }
    # A refused move is a row-level failure, not a run error.
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_a_run_with_nothing_refused_reports_no_failed_updates():
    graph = make_refusing_graph(
        resolved_items=[
            MockDiscovery(id="res-ok", type="bug_found", status="resolved", resolved_at=_old(45)),
        ],
    )
    summary = await KnowledgeGraphLifecycle(graph=graph).run_cleanup(dry_run=False)

    assert summary["discoveries_archived"] == 1
    assert summary["failed_updates"] == {}


@pytest.mark.asyncio
async def test_a_dry_run_reports_candidates_and_attempts_nothing():
    graph = make_refusing_graph(
        refuse={"res-bad"},
        resolved_items=[
            MockDiscovery(id="res-bad", type="bug_found", status="resolved", resolved_at=_old(45)),
        ],
    )
    summary = await KnowledgeGraphLifecycle(graph=graph).run_cleanup(dry_run=True)

    assert summary["discoveries_archived"] == 1
    assert summary["failed_updates"] == {}
    graph.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_tag_rewrite_is_not_counted_as_canonicalized():
    graph = make_refusing_graph(
        refuse={"tag-bad"},
        open_items=[
            MockDiscovery(id="tag-ok", type="note", tags=["db"]),
            MockDiscovery(id="tag-bad", type="note", tags=["auth"]),
        ],
    )
    summary = await KnowledgeGraphLifecycle(graph=graph).run_cleanup(dry_run=False)

    assert summary["tags_canonicalized"] == 1
    assert summary["failed_updates"] == {
        "tags_canonicalized": {"count": 1, "ids": ["tag-bad"]},
    }


@pytest.mark.asyncio
async def test_the_failed_ids_named_are_bounded_and_the_count_is_exact(caplog):
    from src.knowledge_graph_lifecycle import FAILED_ID_SAMPLE_LIMIT

    total = FAILED_ID_SAMPLE_LIMIT + 5
    ids = [f"res-{i:02d}" for i in range(total)]
    graph = make_refusing_graph(
        refuse=set(ids),
        resolved_items=[
            MockDiscovery(id=i, type="bug_found", status="resolved", resolved_at=_old(45))
            for i in ids
        ],
    )

    with caplog.at_level("WARNING", logger="src.knowledge_graph_lifecycle"):
        summary = await KnowledgeGraphLifecycle(graph=graph).run_cleanup(dry_run=False)

    assert summary["discoveries_archived"] == 0
    entry = summary["failed_updates"]["discoveries_archived"]
    assert entry["count"] == total
    assert entry["ids"] == ids[:FAILED_ID_SAMPLE_LIMIT]
    message = next(
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )
    assert f"{total} of {total} moves to archived failed" in message
    assert ids[FAILED_ID_SAMPLE_LIMIT - 1] in message
    assert ids[FAILED_ID_SAMPLE_LIMIT] not in message
    assert "(and 5 more)" in message


@pytest.mark.asyncio
async def test_the_health_record_says_degraded_when_updates_failed(monkeypatch):
    import src.knowledge_graph_lifecycle as lifecycle_module

    graph = make_refusing_graph(
        refuse={"res-bad"},
        resolved_items=[
            MockDiscovery(id="res-bad", type="bug_found", status="resolved", resolved_at=_old(45)),
        ],
    )
    monkeypatch.setattr(
        lifecycle_module.KnowledgeGraphLifecycle, "_get_graph", AsyncMock(return_value=graph)
    )
    monkeypatch.setattr(lifecycle_module, "_refresh_knowledge_nodes_gauge", AsyncMock())
    # The health record is module state; keep this test's write out of others.
    monkeypatch.setattr(
        lifecycle_module,
        "_KG_LIFECYCLE_STATUS",
        {"status": "unknown", "last_run": None, "last_error": None},
    )

    result = await lifecycle_module.run_kg_lifecycle_cleanup(dry_run=False)

    assert result["discoveries_archived"] == 0
    health = lifecycle_module.get_kg_lifecycle_health()
    assert health["status"] == "degraded"
    assert health["last_error"] == "1 lifecycle updates failed (discoveries_archived 1)"


# --- Live, against governance_test ---
#
# Both real backends return False for a row that is gone when its move
# arrives. The candidates are read, one row is deleted, then the pass runs on
# that read: the race where a row disappears between the pass's query and its
# update. The graph a pass sees answers queries from the rows read at the start
# (only this test's rows, since governance_test is shared with other runs) and
# sends every update to the real backend.


class _StaleReadGraph:
    def __init__(self, graph, rows):
        self._graph = graph
        self._rows = rows

    async def query(self, status=None, limit=1000):
        return [row for row in self._rows if row.status == status]

    async def update_discovery(self, discovery_id, updates):
        return await self._graph.update_discovery(discovery_id, updates)


def _live_graphs(backend, monkeypatch):
    """The Postgres backend, and the AGE backend on its SQL-only fallback
    (governance_test has no AGE graph; graph_available is pinned False)."""
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    postgres = KnowledgeGraphPostgres()
    postgres._db = backend
    age = KnowledgeGraphAGE()
    age._db = backend

    async def _no_graph():
        return False

    monkeypatch.setattr(backend, "graph_available", _no_graph)
    return {"postgres": postgres, "age_sql_fallback": age}


async def _live_status(backend, discovery_id):
    async with backend.acquire() as conn:
        return await conn.fetchval(
            "SELECT status FROM knowledge.discoveries WHERE id = $1", discovery_id
        )


async def _live_delete(backend, *discovery_ids):
    async with backend.acquire() as conn:
        await conn.execute(
            "DELETE FROM knowledge.discoveries WHERE id = ANY($1::text[])",
            list(discovery_ids),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["postgres", "age_sql_fallback"])
async def test_live_a_move_the_backend_refuses_is_reported_failed(
    live_postgres_backend, monkeypatch, path
):
    from src.knowledge_graph import DiscoveryNode

    backend = live_postgres_backend
    graph = _live_graphs(backend, monkeypatch)[path]
    kept, gone = f"lifecycle-kept-{path}", f"lifecycle-gone-{path}"
    long_ago = (datetime.now() - timedelta(days=200)).isoformat()
    try:
        for discovery_id in (kept, gone):
            await backend.kg_add_discovery(
                DiscoveryNode(id=discovery_id, agent_id="agent-lifecycle", type="bug_found", summary="s")
            )
            assert await graph.update_discovery(
                discovery_id,
                {"status": "resolved", "resolved_at": long_ago, "updated_at": long_ago},
            )
        rows = [
            row
            for row in await graph.query(status="resolved", limit=1000)
            if row.id in (kept, gone)
        ]
        assert sorted(row.id for row in rows) == sorted([kept, gone])
        await _live_delete(backend, gone)

        lifecycle = KnowledgeGraphLifecycle(graph=_StaleReadGraph(graph, rows))
        summary = await lifecycle.run_cleanup(dry_run=False)

        assert summary["errors"] == []
        assert summary["discoveries_archived"] == 1
        assert summary["failed_updates"] == {
            "discoveries_archived": {"count": 1, "ids": [gone]},
        }
        assert await _live_status(backend, kept) == "archived"
        assert await _live_status(backend, gone) is None
    finally:
        await _live_delete(backend, kept, gone)
