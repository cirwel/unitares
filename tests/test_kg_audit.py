"""
Tests for knowledge graph audit functionality.

Tests run_kg_audit() scoring/bucketing logic and the MCP handler wrapper.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _make_discovery(
    id: str = "disc-1",
    summary: str = "Test discovery",
    type: str = "note",
    status: str = "open",
    agent_id: str = "agent-1",
    tags: List[str] = None,
    age_days: int = 0,
    updated_days_ago: int = None,
    responses_from: List[str] = None,
    related_to: List[str] = None,
) -> SimpleNamespace:
    """Create a mock discovery node."""
    now = datetime.now()
    created = now - timedelta(days=age_days)
    updated = now - timedelta(days=(updated_days_ago if updated_days_ago is not None else age_days))
    return SimpleNamespace(
        id=id,
        summary=summary,
        type=type,
        status=status,
        agent_id=agent_id,
        tags=tags or [],
        timestamp=created.isoformat(),
        updated_at=updated.isoformat(),
        resolved_at=None,
        responses_from=responses_from or [],
        related_to=related_to or [],
    )


def _make_mock_graph(discoveries_by_status: Dict[str, list] = None):
    """Create a mock knowledge graph that returns discoveries by status."""
    if discoveries_by_status is None:
        discoveries_by_status = {"open": [], "resolved": [], "archived": []}

    async def mock_query(status=None, limit=None):
        return discoveries_by_status.get(status, [])

    graph = AsyncMock()
    graph.query = mock_query
    return graph


async def _mock_get_graph(graph):
    """Async factory returning mock graph, for patching get_knowledge_graph."""
    return graph


# =============================================================================
# Tests: _score_discovery
# =============================================================================

class TestScoreDiscovery:
    """Tests for the staleness scoring function."""

    def test_healthy_recent_entry(self):
        """Entry created 2 days ago should be 'healthy'."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=2)
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "healthy"
        assert result["age_days"] == 2

    def test_healthy_with_responses(self):
        """Entry with responses should be 'healthy' regardless of age."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=20, responses_from=["agent-2"])
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "healthy"

    def test_healthy_with_related_links(self):
        """Entry with related_to links should be 'healthy' regardless of age.

        Related-to is a structural anchor — entries cited via related_to are
        load-bearing even without replies. Symmetric with responses_from.
        """
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=60, related_to=["disc-anchor"])
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "healthy"

    def test_aging_entry(self):
        """Entry 10 days old with no activity should be 'aging'."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=10)
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "aging"

    def test_stale_entry(self):
        """Entry 20 days old with no activity should be 'stale'."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=20)
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "stale"

    def test_archive_candidate(self):
        """Entry 45 days old should be 'candidate_for_archive'."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=45)
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "candidate_for_archive"

    def test_permanent_always_healthy(self):
        """Permanent-type entries should always be 'healthy' regardless of age."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=90, type="architecture_decision")
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "healthy"

    def test_permanent_tag_always_healthy(self):
        """Entries tagged 'permanent' should always be 'healthy'."""
        from src.knowledge_graph_lifecycle import _score_discovery, KnowledgeGraphLifecycle
        d = _make_discovery(age_days=60, tags=["permanent"])
        lifecycle = KnowledgeGraphLifecycle()
        result = _score_discovery(d, lifecycle)
        assert result["bucket"] == "healthy"


# =============================================================================
# Tests: run_kg_audit
# =============================================================================

class TestRunKgAudit:
    """Tests for the run_kg_audit() function."""

    @pytest.mark.asyncio
    async def test_audit_returns_structured_report(self):
        """Audit should return a report with required fields."""
        graph = _make_mock_graph({"open": [
            _make_discovery(id="d1", age_days=2),
            _make_discovery(id="d2", age_days=10),
            _make_discovery(id="d3", age_days=25),
        ]})

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(scope="open")

        assert "timestamp" in result
        assert "buckets" in result
        assert "top_stale" in result
        assert "total_audited" in result
        assert result["total_audited"] == 3

    @pytest.mark.asyncio
    async def test_audit_bucket_counts(self):
        """Audit should correctly count entries per bucket."""
        discoveries = [
            _make_discovery(id="healthy1", age_days=1),
            _make_discovery(id="healthy2", age_days=5),
            _make_discovery(id="aging1", age_days=10),
            _make_discovery(id="stale1", age_days=20),
            _make_discovery(id="archive1", age_days=40),
        ]
        graph = _make_mock_graph({"open": discoveries})

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(scope="open")

        assert result["buckets"]["healthy"] == 2
        assert result["buckets"]["aging"] == 1
        assert result["buckets"]["stale"] == 1
        assert result["buckets"]["candidate_for_archive"] == 1

    @pytest.mark.asyncio
    async def test_audit_top_n_stale(self):
        """Should return top_n stale entries sorted by staleness."""
        discoveries = [
            _make_discovery(id="s1", age_days=25),
            _make_discovery(id="s2", age_days=50),
            _make_discovery(id="s3", age_days=35),
            _make_discovery(id="h1", age_days=2),
        ]
        graph = _make_mock_graph({"open": discoveries})

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(scope="open", top_n=2)

        assert len(result["top_stale"]) == 2
        # Most stale first
        assert result["top_stale"][0]["id"] == "s2"
        assert result["top_stale"][1]["id"] == "s3"

    @pytest.mark.asyncio
    async def test_audit_scope_all(self):
        """scope='all' should audit open + resolved + archived."""
        graph = _make_mock_graph({
            "open": [_make_discovery(id="o1", age_days=2)],
            "resolved": [_make_discovery(id="r1", age_days=5)],
            "archived": [_make_discovery(id="a1", age_days=10)],
        })

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(scope="all")

        assert result["total_audited"] == 3

    @pytest.mark.asyncio
    async def test_audit_scope_by_agent(self):
        """scope='by_agent' should only include entries from that agent."""
        graph = _make_mock_graph({
            "open": [
                _make_discovery(id="mine", agent_id="agent-A", age_days=2),
                _make_discovery(id="theirs", agent_id="agent-B", age_days=5),
            ],
        })

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(scope="by_agent", agent_id="agent-A")

        assert result["total_audited"] == 1

    @pytest.mark.asyncio
    async def test_audit_is_read_only(self):
        """Audit should never call update_discovery or any write method."""
        graph = _make_mock_graph({
            "open": [_make_discovery(id="d1", age_days=50)],
        })

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            await run_kg_audit(scope="open")

        # Ensure no writes happened
        graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_empty_graph(self):
        """Audit should handle empty knowledge graph gracefully."""
        graph = _make_mock_graph({"open": []})

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit()

        assert result["total_audited"] == 0
        assert result["top_stale"] == []
        assert all(v == 0 for v in result["buckets"].values())

    @pytest.mark.asyncio
    async def test_audit_use_model_calls_call_model(self):
        """When use_model=True and stale entries exist, call_model is invoked."""
        graph = _make_mock_graph({
            "open": [_make_discovery(id="stale1", age_days=25)],
        })

        mock_response = MagicMock()
        mock_response.text = json.dumps({"success": True, "response": "ARCHIVE: stale entry"})

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph), \
             patch("src.mcp_handlers.support.model_inference.handle_call_model",
                   new_callable=AsyncMock, return_value=[mock_response]) as mock_model:
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(use_model=True)

        mock_model.assert_called_once()
        assert result["model_assessment"] is not None

    @pytest.mark.asyncio
    async def test_audit_use_model_false_skips_model(self):
        """When use_model=False, call_model is not invoked."""
        graph = _make_mock_graph({
            "open": [_make_discovery(id="stale1", age_days=25)],
        })

        with patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=graph):
            from src.knowledge_graph_lifecycle import run_kg_audit
            result = await run_kg_audit(use_model=False)

        assert result["model_assessment"] is None


# =============================================================================
# Tests: handle_audit_knowledge_graph (MCP handler)
# =============================================================================

class TestHandleAuditKnowledgeGraph:
    """Tests for the MCP handler wrapper."""

    @pytest.mark.asyncio
    async def test_handler_returns_success(self):
        """Handler should return success response with audit data."""
        mock_audit = {
            "timestamp": "2026-01-01T00:00:00",
            "scope": "open",
            "total_audited": 5,
            "buckets": {"healthy": 3, "aging": 1, "stale": 1, "candidate_for_archive": 0},
            "top_stale": [],
            "model_assessment": None,
            "thresholds": {"healthy_days": 7, "aging_days": 14, "stale_days": 30},
        }

        with patch("src.knowledge_graph_lifecycle.run_kg_audit",
                   new_callable=AsyncMock, return_value=mock_audit):
            from src.mcp_handlers.knowledge.handlers import handle_audit_knowledge_graph
            result = await handle_audit_knowledge_graph({"scope": "open"})

        # Parse response
        text = result[0].text if hasattr(result[0], "text") else result[0]
        parsed = json.loads(text)
        assert parsed.get("success") is True or "audit" in parsed
        assert "audit" in parsed

    @pytest.mark.asyncio
    async def test_handler_passes_params_correctly(self):
        """Handler should pass scope, top_n, use_model to run_kg_audit."""
        with patch("src.knowledge_graph_lifecycle.run_kg_audit",
                   new_callable=AsyncMock, return_value={
                       "timestamp": "", "scope": "all", "total_audited": 0,
                       "buckets": {}, "top_stale": [], "model_assessment": None,
                       "thresholds": {},
                   }) as mock_audit:
            from src.mcp_handlers.knowledge.handlers import handle_audit_knowledge_graph
            await handle_audit_knowledge_graph({
                "scope": "all",
                "top_n": "5",
                "use_model": "true",
                "agent_id": "test-agent",
            })

        mock_audit.assert_called_once_with(
            scope="all",
            top_n=5,
            use_model=True,
            agent_id="test-agent",
        )

    @pytest.mark.asyncio
    async def test_handler_uses_defaults_for_schema_none_values(self):
        """Unified-tool optional ``None`` values must not erase defaults.

        ``KnowledgeParams.model_dump()`` includes every optional field with a
        value of ``None``.  The MCP path therefore passes ``top_n=None`` even
        when the caller omits it; ``dict.get("top_n", 10)`` does not apply its
        fallback in that case and used to crash at ``int(None)``.
        """
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        arguments = KnowledgeParams(action="audit").model_dump()
        mock_audit_result = {
            "timestamp": "",
            "scope": "open",
            "total_audited": 0,
            "buckets": {},
            "top_stale": [],
            "model_assessment": None,
            "thresholds": {},
        }
        with patch(
            "src.knowledge_graph_lifecycle.run_kg_audit",
            new_callable=AsyncMock,
            return_value=mock_audit_result,
        ) as mock_audit:
            from src.mcp_handlers.knowledge.handlers import handle_audit_knowledge_graph

            result = await handle_audit_knowledge_graph(arguments)

        text = result[0].text if hasattr(result[0], "text") else result[0]
        parsed = json.loads(text)
        assert parsed["success"] is True
        mock_audit.assert_called_once_with(
            scope="open",
            top_n=10,
            use_model=False,
            agent_id=None,
        )

    @pytest.mark.asyncio
    async def test_handler_returns_error_on_failure(self):
        """Handler should return error response on exception."""
        with patch("src.knowledge_graph_lifecycle.run_kg_audit",
                   new_callable=AsyncMock, side_effect=RuntimeError("Graph unavailable")):
            from src.mcp_handlers.knowledge.handlers import handle_audit_knowledge_graph
            result = await handle_audit_knowledge_graph({})

        text = result[0].text if hasattr(result[0], "text") else result[0]
        parsed = json.loads(text)
        assert parsed["success"] is False
        assert "Graph unavailable" in parsed["error"]
