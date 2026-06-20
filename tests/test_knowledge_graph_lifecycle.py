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

    def test_permanent_by_type_architecture_decision(self):
        d = MockDiscovery(id="1", type="architecture_decision")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_learning(self):
        d = MockDiscovery(id="2", type="learning")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_pattern(self):
        d = MockDiscovery(id="3", type="pattern")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_root_cause_analysis(self):
        d = MockDiscovery(id="4", type="root_cause_analysis")
        assert self.lifecycle.get_lifecycle_policy(d) == "permanent"

    def test_permanent_by_type_migration(self):
        d = MockDiscovery(id="5", type="migration")
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
        d = MockDiscovery(id="11", type="architecture_decision", tags=["ephemeral"])
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
        d = MockDiscovery(id="perm1", type="architecture_decision", tags=[],
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
        d = MockDiscovery(id="perm_arch1", type="root_cause_analysis", tags=[],
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
    assert len(PERMANENT_TYPES) >= 4
    assert "architecture_decision" in PERMANENT_TYPES
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

    archived, skipped = await lifecycle._archive_old_resolved(datetime.now(), dry_run=False)

    # The query was called with status='resolved' — superseded is out of band
    mock_graph.query.assert_awaited_with(status="resolved", limit=1000)
    assert archived == []
    assert skipped == 0
    mock_graph.update_discovery.assert_not_awaited()
