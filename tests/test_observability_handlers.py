"""
Tests for src/mcp_handlers/observability.py

Covers:
- handle_observe_agent: Observe a specific agent's governance state
- handle_compare_agents: Compare governance patterns across agents
- handle_compare_me_to_similar: Compare self to similar agents
- handle_detect_anomalies: Detect anomalous agent behavior
- handle_aggregate_metrics: Fleet-level health overview

Each handler is tested for:
- Happy path
- Missing/invalid required arguments
- Error paths (agent not found, etc.)
- Edge cases (no agents, single agent, empty metadata)
"""

import pytest
import json
import asyncio
from typing import Dict, Any, List, Optional, Sequence
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from types import SimpleNamespace
from enum import Enum


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from tests.helpers import parse_result


# ---------------------------------------------------------------------------
# Mock state factory
# ---------------------------------------------------------------------------

class FakeHealthStatus(Enum):
    HEALTHY = "healthy"
    MODERATE = "moderate"
    CRITICAL = "critical"


def _make_state(
    E: float = 0.75,
    I: float = 0.85,
    S: float = 0.15,
    V: float = -0.02,
    coherence: float = 0.65,
    lambda1: float = 0.1,
    update_count: int = 5,
    risk_history: Optional[List[float]] = None,
    void_active: bool = False,
):
    """Build a SimpleNamespace that mimics GovernanceState."""
    return SimpleNamespace(
        E=E,
        I=I,
        S=S,
        V=V,
        coherence=coherence,
        lambda1=lambda1,
        update_count=update_count,
        risk_history=risk_history if risk_history is not None else [0.3, 0.25, 0.2],
        void_active=void_active,
    )


def _make_monitor(agent_id: str = "agent-1", state: Optional[object] = None, metrics: Optional[Dict] = None):
    """Build a mock monitor with a state and get_metrics()."""
    monitor = MagicMock()
    monitor.state = state or _make_state()
    default_metrics = {
        "risk_score": 0.25,
        "current_risk": 0.22,
        "mean_risk": 0.23,
        "phi": 0.6,
        "verdict": "caution",
        "regime": "nominal",
        "status": "healthy",
        "decision_statistics": {
            "proceed": 3,
            "pause": 1,
            "approve": 2,
            "reflect": 1,
            "reject": 0,
            "revise": 0,
        },
    }
    if metrics:
        default_metrics.update(metrics)
    monitor.get_metrics.return_value = default_metrics
    return monitor


def _make_metadata(
    agent_id: str = "agent-1",
    status: str = "active",
    total_updates: int = 5,
    label: Optional[str] = None,
    structured_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
):
    """Build a SimpleNamespace that mimics AgentMetadata."""
    return SimpleNamespace(
        agent_id=agent_id,
        status=status,
        total_updates=total_updates,
        label=label,
        structured_id=structured_id,
        display_name=label,
        tags=tags or [],
        paused_at=None,
        archived_at=None,
    )


def _build_mock_server(
    agent_ids: Optional[List[str]] = None,
    monitors_dict: Optional[Dict] = None,
    metadata_dict: Optional[Dict] = None,
):
    """
    Build a mock mcp_server module with the attributes referenced by observability.py:
    - agent_metadata (dict)
    - monitors (dict)
    - get_or_create_monitor(agent_id)
    - load_metadata()
    - load_metadata_async(force=True)
    - load_monitor_state(agent_id)
    - analyze_agent_patterns(monitor, include_history)
    - health_checker.get_health_status(...)
    """
    server = MagicMock()

    ids = agent_ids or []
    _monitors = monitors_dict or {}
    _metadata = metadata_dict or {}

    # Build defaults if only ids were given
    for aid in ids:
        if aid not in _monitors:
            _monitors[aid] = _make_monitor(aid)
        if aid not in _metadata:
            _metadata[aid] = _make_metadata(aid)

    server.monitors = _monitors
    server.agent_metadata = _metadata

    def _get_or_create_monitor(aid):
        if aid not in _monitors:
            _monitors[aid] = _make_monitor(aid)
        return _monitors[aid]

    server.get_or_create_monitor = MagicMock(side_effect=_get_or_create_monitor)
    server.load_metadata = MagicMock()
    server.load_metadata_async = AsyncMock()
    server.load_monitor_state = MagicMock(return_value=None)

    # analyze_agent_patterns returns an observation dict
    server.analyze_agent_patterns = MagicMock(return_value={
        "current_state": {"E": 0.75, "I": 0.85, "S": 0.15, "V": -0.02},
        "patterns": ["stable"],
        "anomalies": [],
    })

    # health_checker
    server.health_checker = MagicMock()
    server.health_checker.get_health_status.return_value = (FakeHealthStatus.HEALTHY, "healthy")

    return server


# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------

# The observability module captures `mcp_server` at import-time via
#   `from .shared import get_mcp_server; mcp_server = get_mcp_server()`
# So we must patch the module-level `mcp_server` attribute.
_OBS_MOD = "src.mcp_handlers.observability.handlers"
_PATCH_SERVER = f"{_OBS_MOD}.mcp_server"
_PATCH_CTX = "src.mcp_handlers.context.get_context_agent_id"
# require_registered_agent (used by compare_me_to_similar) calls get_mcp_server()
# internally.  We must also patch that so it sees our mock server.
_PATCH_GET_MCP_SERVER = "src.mcp_handlers.shared.get_mcp_server"


# ---------------------------------------------------------------------------
# handle_observe_agent
# ---------------------------------------------------------------------------

class TestHandleObserveAgent:
    """Tests for handle_observe_agent."""

    @pytest.mark.asyncio
    async def test_happy_path_with_uuid(self):
        """Observe an agent by UUID -- success path."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server(agent_ids=[uuid])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="other-caller-uuid"):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": uuid})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid
        assert "observation" in data

    @pytest.mark.asyncio
    async def test_happy_path_without_pattern_analysis(self):
        """Observe an agent with analyze_patterns=False -- returns raw state."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server(agent_ids=[uuid])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="other-caller-uuid"):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({
                "target_agent_id": uuid,
                "analyze_patterns": False,
            })

        data = parse_result(result)
        assert data["success"] is True
        obs = data["observation"]
        assert "current_state" in obs
        state = obs["current_state"]
        assert "E" in state
        assert "coherence" in state

    @pytest.mark.asyncio
    async def test_missing_target_agent_id(self):
        """No target_agent_id and no fallback -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({})

        data = parse_result(result)
        assert data["success"] is False
        assert "target_agent_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_agent_id_fallback_when_differs_from_caller(self):
        """agent_id parameter used when it differs from caller's session UUID."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server(agent_ids=[uuid])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="different-caller-uuid-0000-000000000000"):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"agent_id": uuid})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid

    @pytest.mark.asyncio
    async def test_agent_id_same_as_caller_returns_error(self):
        """agent_id matching caller's session ID should not be used as target."""
        caller_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=caller_uuid):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"agent_id": caller_uuid})

        data = parse_result(result)
        # agent_id == caller UUID means no target resolved
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_agent_not_in_metadata(self):
        """Target UUID not in metadata -> error after loading."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server()  # empty metadata

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="caller-uuid"):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": uuid})

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_label_resolution(self):
        """When target is a label (not UUID format), resolve via _find_agent_by_label."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server(agent_ids=[uuid])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="caller-uuid"), \
             patch(
                 "src.mcp_handlers.identity.handlers._find_agent_by_label",
                 new_callable=AsyncMock,
                 return_value=uuid,
             ):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": "Lumen"})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid

    @pytest.mark.asyncio
    async def test_label_resolution_metadata_fallback(self):
        """When _find_agent_by_label returns None, fall back to metadata label search."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        meta = _make_metadata(uuid, label="Lumen")
        server = _build_mock_server(
            monitors_dict={uuid: _make_monitor(uuid)},
            metadata_dict={uuid: meta},
        )

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="caller-uuid"), \
             patch(
                 "src.mcp_handlers.identity.handlers._find_agent_by_label",
                 new_callable=AsyncMock,
                 return_value=None,
             ):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": "Lumen"})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid

    @pytest.mark.asyncio
    async def test_label_not_found(self):
        """Label lookup returns None and no metadata match -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="caller-uuid"), \
             patch(
                 "src.mcp_handlers.identity.handlers._find_agent_by_label",
                 new_callable=AsyncMock,
                 return_value=None,
             ):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": "NoSuchAgent"})

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()


# ---------------------------------------------------------------------------
# handle_compare_agents
# ---------------------------------------------------------------------------

class TestHandleCompareAgents:
    """Tests for handle_compare_agents."""

    @pytest.mark.asyncio
    async def test_happy_path_two_agents(self):
        """Compare two agents -> success with comparison data."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({"agent_ids": [id1, id2]})

        data = parse_result(result)
        assert data["success"] is True
        comparison = data["comparison"]
        assert len(comparison["agents"]) == 2
        assert "similarities" in comparison
        assert "outliers" in comparison

    @pytest.mark.asyncio
    async def test_fewer_than_two_agent_ids(self):
        """Less than 2 agent_ids -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({"agent_ids": ["only-one"]})

        data = parse_result(result)
        assert data["success"] is False
        assert "at least 2" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_empty_agent_ids(self):
        """Empty agent_ids list -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({"agent_ids": []})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_no_agent_ids_key(self):
        """Missing agent_ids key entirely -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_agents_not_found_returns_error(self):
        """2 agent_ids given but neither has data -> error (< 2 with data)."""
        server = _build_mock_server()  # empty monitors and metadata

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({
                "agent_ids": [
                    "aaaaaaaa-bbbb-cccc-dddd-111111111111",
                    "aaaaaaaa-bbbb-cccc-dddd-222222222222",
                ]
            })

        data = parse_result(result)
        assert data["success"] is False
        assert "could not load" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_custom_compare_metrics(self):
        """Custom compare_metrics parameter."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({
                "agent_ids": [id1, id2],
                "compare_metrics": ["coherence", "E"],
            })

        data = parse_result(result)
        assert data["success"] is True
        assert len(data["comparison"]["agents"]) == 2

    @pytest.mark.asyncio
    async def test_three_agents_with_outlier(self):
        """Three agents with one outlier in risk_score."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        id3 = "aaaaaaaa-bbbb-cccc-dddd-333333333333"

        # id3 has wildly different metrics -> potential outlier
        m3 = _make_monitor(id3, metrics={"risk_score": 0.95, "current_risk": 0.95})
        m3.state = _make_state(E=0.1, I=0.2, S=0.9, coherence=0.1)

        monitors = {
            id1: _make_monitor(id1),
            id2: _make_monitor(id2),
            id3: m3,
        }
        metadata = {
            id1: _make_metadata(id1),
            id2: _make_metadata(id2),
            id3: _make_metadata(id3),
        }
        server = _build_mock_server(monitors_dict=monitors, metadata_dict=metadata)

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({"agent_ids": [id1, id2, id3]})

        data = parse_result(result)
        assert data["success"] is True
        assert len(data["comparison"]["agents"]) == 3

    @pytest.mark.asyncio
    async def test_label_resolution_in_compare(self):
        """Labels in agent_ids list get resolved to UUIDs."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[uuid, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.mcp_handlers.identity.handlers._find_agent_by_label",
                 new_callable=AsyncMock,
                 return_value=uuid,
             ):
            from src.mcp_handlers.observability.handlers import handle_compare_agents
            result = await handle_compare_agents({"agent_ids": ["Lumen", id2]})

        data = parse_result(result)
        assert data["success"] is True


# ---------------------------------------------------------------------------
# handle_compare_me_to_similar
# ---------------------------------------------------------------------------

class TestHandleCompareMeToSimilar:
    """Tests for handle_compare_me_to_similar."""

    def _setup_server_with_similar(
        self,
        my_uuid: str,
        others: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        """
        Build a server where `my_uuid` has active metadata and monitors,
        plus optionally other agents with custom EISV values.
        """
        monitors_dict = {}  # type: Dict[str, Any]
        metadata_dict = {}  # type: Dict[str, Any]

        # Me
        my_monitor = _make_monitor(my_uuid)
        my_monitor.state = _make_state(E=0.75, I=0.85, S=0.15, coherence=0.65)
        my_monitor.get_metrics.return_value = {
            "E": 0.75, "I": 0.85, "S": 0.15, "coherence": 0.65,
            "phi": 0.6, "verdict": "caution", "regime": "nominal",
            "risk_score": 0.25,
        }
        monitors_dict[my_uuid] = my_monitor
        metadata_dict[my_uuid] = _make_metadata(
            my_uuid, status="active", total_updates=10,
            label="TestAgent", structured_id="test_agent_id",
        )

        if others:
            for oid, vals in others.items():
                om = _make_monitor(oid)
                om.state = _make_state(
                    E=vals.get("E", 0.75),
                    I=vals.get("I", 0.85),
                    S=vals.get("S", 0.15),
                    coherence=vals.get("coherence", 0.65),
                )
                om.get_metrics.return_value = {
                    "E": vals.get("E", 0.75),
                    "I": vals.get("I", 0.85),
                    "S": vals.get("S", 0.15),
                    "coherence": vals.get("coherence", 0.65),
                    "phi": vals.get("phi", 0.6),
                    "verdict": vals.get("verdict", "caution"),
                    "regime": vals.get("regime", "nominal"),
                    "risk_score": vals.get("risk_score", 0.25),
                }
                monitors_dict[oid] = om
                metadata_dict[oid] = _make_metadata(
                    oid, status="active", total_updates=int(vals.get("total_updates", 5)),
                )

        return _build_mock_server(monitors_dict=monitors_dict, metadata_dict=metadata_dict)

    def _patches(self, server, ctx_return):
        """Return a combined context manager patching mcp_server, get_mcp_server, and context."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch(_PATCH_SERVER, server))
        stack.enter_context(patch(_PATCH_GET_MCP_SERVER, return_value=server))
        stack.enter_context(patch(_PATCH_CTX, return_value=ctx_return))
        return stack

    @pytest.mark.asyncio
    async def test_happy_path_with_similar_agents(self):
        """Find similar agents and compare."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.76, "I": 0.84, "S": 0.16, "coherence": 0.64,
                "total_updates": 8,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True
        assert "similar_agents" in data
        assert len(data["similar_agents"]) >= 1

    @pytest.mark.asyncio
    async def test_no_similar_agents(self):
        """No other agents match -> returns message about no similar agents."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        # Very different other agent
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.1, "I": 0.1, "S": 0.9, "coherence": 0.1,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True
        # Should either have no similar_agents or a message
        if "similar_agents" in data:
            assert len(data["similar_agents"]) == 0 or "no similar" in data.get("message", "").lower()
        else:
            assert "no similar" in data.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_no_other_agents_at_all(self):
        """Only the calling agent exists -> no similar found."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        server = self._setup_server_with_similar(my_uuid, others=None)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True
        assert "no similar" in data.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_unregistered_agent_returns_error(self):
        """Agent not in metadata -> require_registered_agent fails."""
        server = _build_mock_server()  # empty

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_GET_MCP_SERVER, return_value=server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_custom_similarity_threshold(self):
        """Wider threshold includes more agents."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        # Agent with moderate difference
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.6, "I": 0.7, "S": 0.3, "coherence": 0.5,
                "total_updates": 5,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            # Very wide threshold should include the agent
            result = await handle_compare_me_to_similar({
                "agent_id": my_uuid,
                "similarity_threshold": 0.5,
            })

        data = parse_result(result)
        assert data["success"] is True
        if "similar_agents" in data:
            assert len(data["similar_agents"]) >= 1

    @pytest.mark.asyncio
    async def test_insights_generated(self):
        """When similar agents exist, insights list is populated."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.76, "I": 0.84, "S": 0.16, "coherence": 0.64,
                "verdict": "caution", "phi": 0.6, "total_updates": 8,
            },
            "aaaaaaaa-bbbb-cccc-dddd-222222222222": {
                "E": 0.74, "I": 0.86, "S": 0.14, "coherence": 0.66,
                "verdict": "caution", "phi": 0.62, "total_updates": 12,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True
        # insights is always present -- may contain pattern, individual, or summary insights
        assert "insights" in data
        assert isinstance(data["insights"], list)

    @pytest.mark.asyncio
    async def test_compare_me_returns_success(self):
        """Response is successful with comparison data."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.76, "I": 0.84, "S": 0.16, "coherence": 0.64,
                "total_updates": 8,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True


# ---------------------------------------------------------------------------
# handle_detect_anomalies
# ---------------------------------------------------------------------------

class TestHandleDetectAnomalies:
    """Tests for handle_detect_anomalies."""

    @pytest.mark.asyncio
    async def test_happy_path_no_anomalies(self):
        """Scan agents, find no anomalies."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value={"anomalies": []},
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["summary"]["total_anomalies"] == 0

    @pytest.mark.asyncio
    async def test_happy_path_with_anomalies(self):
        """Scan agents, find anomalies that match type and severity."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        anomaly_data = {
            "anomalies": [
                {"type": "risk_spike", "severity": "high", "description": "Risk spiked"},
                {"type": "coherence_drop", "severity": "medium", "description": "Coherence dropped"},
                {"type": "risk_spike", "severity": "low", "description": "Minor risk spike"},
            ]
        }

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value=anomaly_data,
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({
                "anomaly_types": ["risk_spike", "coherence_drop"],
                "min_severity": "medium",
            })

        data = parse_result(result)
        assert data["success"] is True
        # Only high and medium should pass the filter
        assert data["summary"]["total_anomalies"] == 2
        assert data["summary"]["by_severity"]["high"] == 1
        assert data["summary"]["by_severity"]["medium"] == 1

    @pytest.mark.asyncio
    async def test_audit_writes_per_agent_fanout(self):
        """Each detected anomaly writes its own audit entry with the affected
        agent_id — not a single batch entry with agent_id='system'.

        Regression guard for the PPV pipeline: the Schmidt-proposal figure
        and future v7 correlation work join audit.events.agent_id against
        lifecycle_paused. The prior batched-write shape collapsed every
        anomaly into one `agent_id='system'` row and truncated details at 10.
        """
        ids = [
            "aaaaaaaa-bbbb-cccc-dddd-000000000001",
            "aaaaaaaa-bbbb-cccc-dddd-000000000002",
            "aaaaaaaa-bbbb-cccc-dddd-000000000003",
        ]
        server = _build_mock_server(agent_ids=ids)

        # Fresh anomaly dicts per call — handlers.py mutates anomaly["agent_id"]
        # in-place (line ~581), so a shared dict would get overwritten.
        # Types must match the filter accepted by the handler (risk_spike /
        # coherence_drop).
        def patterns_side_effect(*_args, **_kwargs):
            return {
                "anomalies": [
                    {"type": "risk_spike", "severity": "high",
                     "description": "spike 1"},
                    {"type": "risk_spike", "severity": "high",
                     "description": "spike 2"},
                    {"type": "coherence_drop", "severity": "high",
                     "description": "drop 1"},
                    {"type": "coherence_drop", "severity": "high",
                     "description": "drop 2"},
                ]
            }

        from src.audit_log import AuditEntry

        written: list[AuditEntry] = []

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 side_effect=patterns_side_effect,
             ), \
             patch("src.event_detector.event_detector.record_event",
                   side_effect=lambda ev: ev), \
             patch("src.audit_log.audit_logger._write_entry",
                   side_effect=lambda entry: written.append(entry)):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["summary"]["total_anomalies"] == 12  # 3 agents × 4

        # One audit entry per anomaly — no truncation at 10.
        assert len(written) == 12, f"expected 12 fan-out entries, got {len(written)}"

        # Every entry carries the real affected agent_id, not 'system'.
        assert all(e.event_type == "anomaly_detected" for e in written)
        assert all(e.agent_id in ids for e in written), (
            f"audit entries must carry the affected agent_id: "
            f"{[e.agent_id for e in written]}"
        )
        assert not any(e.agent_id == "system" for e in written)

        # Each entry's details describe a single anomaly (not a list).
        for e in written:
            assert "type" in e.details
            assert "severity" in e.details
            assert "anomalies" not in e.details, (
                "details should be per-anomaly, not a batch list"
            )

    @pytest.mark.asyncio
    async def test_specific_agent_ids(self):
        """Scan only specified agent_ids."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value={"anomalies": []},
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_no_active_agents(self):
        """No active agents -> empty anomalies."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["summary"]["total_anomalies"] == 0

    @pytest.mark.asyncio
    async def test_anomaly_type_filtering(self):
        """Only anomalies matching requested types are returned."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        anomaly_data = {
            "anomalies": [
                {"type": "risk_spike", "severity": "high", "description": "Risk spiked"},
                {"type": "coherence_drop", "severity": "high", "description": "Coherence dropped"},
            ]
        }

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value=anomaly_data,
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({
                "anomaly_types": ["risk_spike"],
                "min_severity": "low",
            })

        data = parse_result(result)
        assert data["success"] is True
        # Only risk_spike should pass
        assert data["summary"]["total_anomalies"] == 1
        assert data["summary"]["by_type"].get("risk_spike", 0) == 1

    @pytest.mark.asyncio
    async def test_anomalies_sorted_by_severity(self):
        """Anomalies should be sorted high -> medium -> low."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        anomaly_data = {
            "anomalies": [
                {"type": "risk_spike", "severity": "low", "description": "Low"},
                {"type": "risk_spike", "severity": "high", "description": "High"},
                {"type": "risk_spike", "severity": "medium", "description": "Medium"},
            ]
        }

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value=anomaly_data,
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({
                "min_severity": "low",
            })

        data = parse_result(result)
        assert data["success"] is True
        severities = [a["severity"] for a in data["anomalies"]]
        # High should be first
        assert severities[0] == "high"

    @pytest.mark.asyncio
    async def test_monitor_loaded_from_persisted_state(self):
        """When monitor is not in memory, load from persisted state."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        # Server with metadata but no in-memory monitor
        metadata = {id1: _make_metadata(id1)}
        server = _build_mock_server(metadata_dict=metadata)
        # Simulate persisted state being found
        server.load_monitor_state = MagicMock(return_value=_make_state())

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value={"anomalies": []},
             ), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitor:
            mock_instance = _make_monitor(id1)
            MockMonitor.return_value = mock_instance
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_exception_in_agent_processing_continues(self):
        """Exceptions in individual agent processing are caught; other agents still processed."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        call_count = [0]

        def _patched_analyze(monitor, include_history):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Boom!")
            return {"anomalies": []}

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 side_effect=_patched_analyze,
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({"agent_ids": [id1, id2]})

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeated_audit_writes(self):
        """Calling detect_anomalies twice with the same condition should write to audit only once."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        anomaly_data = {
            "anomalies": [
                {"type": "risk_spike", "severity": "medium", "description": "Risk spiked"},
            ]
        }

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value=anomaly_data,
             ), \
             patch("src.audit_log.audit_logger") as mock_audit:
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies

            # First call — anomaly is new, should write audit entry
            result1 = await handle_detect_anomalies({})
            data1 = parse_result(result1)
            assert data1["success"] is True
            assert data1["summary"]["total_anomalies"] == 1
            first_audit_count = mock_audit._write_entry.call_count

            # Second call — same fingerprint within dedup window, no new audit write
            result2 = await handle_detect_anomalies({})
            data2 = parse_result(result2)
            assert data2["success"] is True
            # Response still shows all current anomalies
            assert data2["summary"]["total_anomalies"] == 1
            # Audit write count should not have increased
            assert mock_audit._write_entry.call_count == first_audit_count

    @pytest.mark.asyncio
    async def test_anomaly_event_payload_includes_agent_name_and_message(self):
        """Anomaly broadcast payloads carry display data for Discord embeds."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        metadata = {id1: _make_metadata(id1, label="Iris")}
        server = _build_mock_server(agent_ids=[id1], metadata_dict=metadata)
        recorded = []

        anomaly_data = {
            "anomalies": [
                {
                    "type": "coherence_drop",
                    "severity": "high",
                    "description": "Coherence dropped from 0.48 to 0.36 (0.12 change)",
                },
            ]
        }

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value=anomaly_data,
             ), \
             patch(
                 "src.event_detector.event_detector.record_event",
                 side_effect=lambda event: recorded.append(event) or event,
             ), \
             patch("src.audit_log.audit_logger"):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            result = await handle_detect_anomalies({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True
        assert recorded
        assert recorded[0]["agent_id"] == id1
        assert recorded[0]["agent_name"] == "Iris"
        assert recorded[0]["message"] == (
            "Iris: Coherence dropped from 0.48 to 0.36 (0.12 change)"
        )

    @pytest.mark.asyncio
    async def test_does_not_force_full_metadata_reload(self):
        """Regression: handle_detect_anomalies MUST NOT call load_metadata_async
        with force=True. Same anti-pattern as handle_aggregate_metrics; see
        Wave 0 follow-up to PR #348."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value={"anomalies": []},
             ):
            from src.mcp_handlers.observability.handlers import handle_detect_anomalies
            await handle_detect_anomalies({})

        for call in server.load_metadata_async.await_args_list:
            assert call.kwargs.get("force", False) is False, (
                f"handle_detect_anomalies called load_metadata_async with "
                f"force=True (kwargs={call.kwargs})"
            )
            for arg in call.args:
                assert arg is not True, (
                    f"handle_detect_anomalies called load_metadata_async "
                    f"with positional True (args={call.args})"
                )


# ---------------------------------------------------------------------------
# handle_aggregate_metrics
# ---------------------------------------------------------------------------

class _FakeAggregateConn:
    """Routes the four queries handle_aggregate_metrics issues to canned rows.

    The handler issues:
      1. epoch lookup (fetchrow)
      2. state rollup (fetchrow)        — fields: total_agents, agents_with_data, ...
      3. total_updates (fetchval)
      4. pauses_this_epoch (fetchval)   — lifecycle_paused count
      5. proceed_this_epoch (fetchval)  — trajectory_validated count
      6. verdict distribution (fetch)
    """

    def __init__(
        self,
        *,
        epoch: int = 3,
        epoch_started_at=None,
        state_row=None,
        total_updates: int = 0,
        pauses_this_epoch: int = 0,
        proceed_this_epoch: int = 0,
        verdict_rows=None,
    ):
        from datetime import datetime, timezone
        self._epoch = epoch
        self._epoch_started_at = epoch_started_at or datetime(
            2026, 4, 27, tzinfo=timezone.utc
        )
        self._state_row = state_row or {
            "total_agents": 0,
            "agents_with_data": 0,
            "mean_risk_score": 0.0,
            "mean_coherence": 0.0,
            "healthy": 0,
            "moderate": 0,
            "critical": 0,
            "unknown_health": 0,
            "paused_now": 0,
            "staleness_oldest_seconds": 0,
            "staleness_newest_seconds": 0,
        }
        self._total_updates = total_updates
        self._pauses_this_epoch = pauses_this_epoch
        self._proceed_this_epoch = proceed_this_epoch
        self._verdict_rows = verdict_rows or []
        # captured args so tests can assert scoping
        self.fetchrow_calls = []
        self.fetchval_calls = []
        self.fetch_calls = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        if "core.epochs" in sql:
            return {
                "epoch": self._epoch,
                "started_at": self._epoch_started_at,
            }
        # state rollup
        return self._state_row

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        if "metadata->>'total_updates'" in sql or "total_updates" in sql:
            return self._total_updates
        if "lifecycle_paused" in sql:
            return self._pauses_this_epoch
        if "trajectory_validated" in sql:
            return self._proceed_this_epoch
        return 0

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._verdict_rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def _patch_db(conn):
    """Patch get_db at its source so the in-function import resolves to fake."""
    fake_db = _FakeDB(conn)
    return patch("src.db.get_db", return_value=fake_db)


class TestHandleAggregateMetrics:
    """Tests for handle_aggregate_metrics — Postgres-canonical path.

    The handler queries core.mv_latest_agent_states, core.agents,
    core.identities.metadata, audit.events (lifecycle_paused),
    audit.outcome_events (trajectory_validated), and audit.r1_score_audit.
    These tests mock the DB connection — the prior in-memory monitor
    iteration was replaced because it produced "0 pauses" reports while
    audit.events had hundreds.
    """

    @pytest.mark.asyncio
    async def test_happy_path(self):
        conn = _FakeAggregateConn(
            state_row={
                "total_agents": 2,
                "agents_with_data": 2,
                "mean_risk_score": 0.25,
                "mean_coherence": 0.5,
                "healthy": 1,
                "moderate": 1,
                "critical": 0,
                "unknown_health": 0,
                "paused_now": 0,
                "staleness_oldest_seconds": 100,
                "staleness_newest_seconds": 10,
            },
            total_updates=137000,
            pauses_this_epoch=81,
            proceed_this_epoch=13000,
        )
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        assert data["success"] is True
        agg = data["aggregate"]
        assert agg["total_agents"] == 2
        assert agg["agents_with_data"] == 2
        assert agg["mean_risk_score"] == pytest.approx(0.25)
        assert agg["mean_coherence"] == pytest.approx(0.5)
        assert agg["total_updates"] == 137000
        assert agg["pauses_this_epoch"] == 81
        assert agg["paused_now"] == 0
        assert agg["epoch"] == 3
        assert "as_of" in agg
        assert "staleness" in agg
        assert agg["decision_distribution"]["pause"] == 81
        assert agg["decision_distribution"]["proceed"] == 13000

    @pytest.mark.asyncio
    async def test_no_active_agents(self):
        conn = _FakeAggregateConn()  # all zeros by default
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})
        data = parse_result(result)
        agg = data["aggregate"]
        assert agg["total_agents"] == 0
        assert agg["agents_with_data"] == 0
        assert agg["mean_risk_score"] == 0.0
        assert agg["mean_coherence"] == 0.0
        assert agg["pauses_this_epoch"] == 0

    @pytest.mark.asyncio
    async def test_specific_agent_ids_scopes_query(self):
        """When agent_ids passed, the SQL uses WHERE a.id = ANY($1::text[])."""
        conn = _FakeAggregateConn(
            state_row={**_FakeAggregateConn().__dict__["_state_row"], "total_agents": 1, "agents_with_data": 1},
        )
        ids = ["aaaaaaaa-bbbb-cccc-dddd-111111111111"]
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            await handle_aggregate_metrics({"agent_ids": ids})

        # First fetchrow after epoch lookup is the state rollup with the scope
        state_call = [c for c in conn.fetchrow_calls if "scope" in c[0]][0]
        assert "ANY($1::text[])" in state_call[0]
        assert state_call[1] == (ids,)

    @pytest.mark.asyncio
    async def test_health_breakdown_excluded(self):
        conn = _FakeAggregateConn()
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({"include_health_breakdown": False})
        data = parse_result(result)
        assert "health_breakdown" not in data["aggregate"]

    @pytest.mark.asyncio
    async def test_pauses_persist_even_when_agents_freshly_loaded(self):
        """Regression: this is the load-bearing fix. The prior in-memory path
        reported pauses=0 when no agent in this process had been written-through
        since startup, despite audit.events showing real pauses. The new path
        always reads audit.events directly, so pauses surface regardless of
        process state."""
        conn = _FakeAggregateConn(
            state_row={
                "total_agents": 100,
                "agents_with_data": 0,  # nothing in matview for this scope
                "mean_risk_score": 0.0,
                "mean_coherence": 0.0,
                "healthy": 0, "moderate": 0, "critical": 0, "unknown_health": 100,
                "paused_now": 5,
                "staleness_oldest_seconds": 0,
                "staleness_newest_seconds": 0,
            },
            pauses_this_epoch=83,  # the real audit.events count from the bug report
        )
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})
        data = parse_result(result)
        agg = data["aggregate"]
        assert agg["pauses_this_epoch"] == 83
        assert agg["paused_now"] == 5
        assert agg["decision_distribution"]["pause"] == 83

    @pytest.mark.asyncio
    async def test_verdict_mapping_r1_to_legacy(self):
        """r1_score_audit verdicts (plausible/inconclusive/unsupported) map to
        legacy verdict_distribution keys (safe/caution/high-risk)."""
        conn = _FakeAggregateConn(
            verdict_rows=[
                {"verdict": "plausible", "n": 10},
                {"verdict": "inconclusive", "n": 4},
                {"verdict": "unsupported", "n": 1},
            ],
        )
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})
        v = parse_result(result)["aggregate"]["verdict_distribution"]
        assert v["safe"] == 10
        assert v["caution"] == 4
        assert v["high-risk"] == 1
        assert v["total"] == 15

    @pytest.mark.asyncio
    async def test_response_includes_freshness_metadata(self):
        """as_of, epoch, and staleness must be present so callers know the
        freshness window. The prior handler reported in-memory state as fleet
        truth with no freshness signal — that's the ontology bug being fixed."""
        conn = _FakeAggregateConn(
            state_row={
                "total_agents": 1, "agents_with_data": 1,
                "mean_risk_score": 0.3, "mean_coherence": 0.5,
                "healthy": 1, "moderate": 0, "critical": 0, "unknown_health": 0,
                "paused_now": 0,
                "staleness_oldest_seconds": 42,
                "staleness_newest_seconds": 5,
            },
        )
        with _patch_db(conn):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})
        agg = parse_result(result)["aggregate"]
        assert agg["epoch"] == 3
        assert "as_of" in agg and agg["as_of"]
        assert agg["staleness"]["oldest_state_seconds"] == 42
        assert agg["staleness"]["newest_state_seconds"] == 5


# ---------------------------------------------------------------------------
# handle_observe (unified router) -- test via consolidated module
# ---------------------------------------------------------------------------

class TestHandleObserveRouter:
    """Tests for the unified handle_observe action router."""

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        """No action parameter -> error listing available actions."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        """Unknown action -> error."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({"action": "nonexistent"})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_agent_action_delegates_to_observe_agent(self):
        """action='agent' routes to handle_observe_agent."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        server = _build_mock_server(agent_ids=[uuid])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="other-caller-uuid"):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({
                "action": "agent",
                "target_agent_id": uuid,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid

    @pytest.mark.asyncio
    async def test_aggregate_action_delegates(self):
        """action='aggregate' routes to handle_aggregate_metrics."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])
        conn = _FakeAggregateConn()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             _patch_db(conn):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({"action": "aggregate"})

        data = parse_result(result)
        assert data["success"] is True
        assert "aggregate" in data

    @pytest.mark.asyncio
    async def test_compare_action_delegates(self):
        """action='compare' routes to handle_compare_agents."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({
                "action": "compare",
                "agent_ids": [id1, id2],
            })

        data = parse_result(result)
        assert data["success"] is True
        assert "comparison" in data

    @pytest.mark.asyncio
    async def test_anomalies_action_delegates(self):
        """action='anomalies' routes to handle_detect_anomalies."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch(
                 "src.pattern_analysis.analyze_agent_patterns",
                 return_value={"anomalies": []},
             ):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({"action": "anomalies"})

        data = parse_result(result)
        assert data["success"] is True
        assert "summary" in data


# ---------------------------------------------------------------------------
# Audit events (issue #422)
# ---------------------------------------------------------------------------

class TestHandleAuditEvents:
    """Tests for observe(action='audit_events') — issue #422."""

    @staticmethod
    def _fake_event(ts: str, agent_id: str, event_type: str = "continuity_token_deprecated_accept", **payload):
        return {
            "timestamp": ts,
            "agent_id": agent_id,
            "event_type": event_type,
            "confidence": 1.0,
            "details": payload or {},
            "event_id": f"evt-{ts}-{agent_id}",
        }

    @pytest.mark.asyncio
    async def test_missing_event_type_returns_error(self):
        from src.mcp_handlers.observability.handlers import handle_audit_events
        result = await handle_audit_events({})
        data = parse_result(result)
        assert data["success"] is False
        assert "event_type" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_shorthand_window_resolves_correctly(self):
        """'14d' → start_time exactly 14 days ago (UTC)."""
        captured: Dict[str, Any] = {}

        async def fake_query(**kwargs):
            captured.update(kwargs)
            return []

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({
                "event_type": "continuity_token_deprecated_accept",
                "since": "14d",
            })

        from datetime import datetime, timezone, timedelta
        start = datetime.fromisoformat(captured["start_time"])
        expected = datetime.now(timezone.utc) - timedelta(days=14)
        # Allow small drift between the call and now()
        assert abs((start - expected).total_seconds()) < 5
        assert captured["event_type"] == "continuity_token_deprecated_accept"
        data = parse_result(result)
        assert data["success"] is True
        assert data["total_emits"] == 0
        assert data["raw_row_count"] == 0

    @pytest.mark.asyncio
    async def test_negative_shorthand_rejected(self):
        """`since='-14d'` would silently query a future window — reject."""
        from src.mcp_handlers.observability.handlers import handle_audit_events
        result = await handle_audit_events({"event_type": "x", "since": "-14d"})
        data = parse_result(result)
        assert data["success"] is False
        assert "negative" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_default_window_is_seven_days(self):
        """Default since= → 7d window (matches primary grace-window use case)."""
        captured: Dict[str, Any] = {}

        async def fake_query(**kwargs):
            captured.update(kwargs)
            return []

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({"event_type": "x"})

        from datetime import datetime, timezone, timedelta
        start = datetime.fromisoformat(captured["start_time"])
        expected = datetime.now(timezone.utc) - timedelta(days=7)
        assert abs((start - expected).total_seconds()) < 5
        data = parse_result(result)
        assert data["window"]["defaulted"] is True

    @pytest.mark.asyncio
    async def test_event_types_wins_when_both_provided(self):
        """When both event_type and event_types are passed, event_types wins.

        The response payload echoes the *effective* filter, not the raw inputs,
        so callers see what was actually queried.
        """
        captured: Dict[str, Any] = {}

        async def fake_query(**kwargs):
            captured.update(kwargs)
            return []

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({
                "event_type": "foo",
                "event_types": ["bar", "baz"],
                "since": "7d",
            })

        assert captured["event_type"] is None
        assert captured["event_types"] == ["bar", "baz"]
        data = parse_result(result)
        assert data["event_type"] is None
        assert data["event_types"] == ["bar", "baz"]

    @pytest.mark.asyncio
    async def test_grouping_by_agent(self):
        events = [
            self._fake_event("2026-05-19T10:00:00+00:00", "real-agent-uuid-1"),
            self._fake_event("2026-05-19T11:00:00+00:00", "real-agent-uuid-1"),
            self._fake_event("2026-05-19T12:00:00+00:00", "Test_Agent_S9"),
        ]

        async def fake_query(**kwargs):
            return events

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({
                "event_type": "continuity_token_deprecated_accept",
                "since": "7d",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["raw_row_count"] == 3
        assert data["total_emits"] == 3  # include_test_fixtures default True
        assert data["test_fixture_emits"] == 1
        assert data["by_agent_id"] == {"real-agent-uuid-1": 2, "Test_Agent_S9": 1}
        assert data["first_ts"] == "2026-05-19T10:00:00+00:00"
        assert data["last_ts"] == "2026-05-19T12:00:00+00:00"

    @pytest.mark.asyncio
    async def test_exclude_test_fixtures(self):
        # Mixed-case `test_` prefix variants verified against live audit.events
        # 2026-05-20 — handler should catch all of them.
        events = [
            self._fake_event("2026-05-19T10:00:00+00:00", "real-agent-uuid-1"),
            self._fake_event("2026-05-19T11:00:00+00:00", "Test_Agent_S9"),       # title case
            self._fake_event("2026-05-19T12:00:00+00:00", "test_agent"),         # lowercase
            self._fake_event("2026-05-19T13:00:00+00:00", "test_stress"),        # lowercase variant
        ]

        async def fake_query(**kwargs):
            return events

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({
                "event_type": "continuity_token_deprecated_accept",
                "since": "7d",
                "include_test_fixtures": False,
            })

        data = parse_result(result)
        assert data["raw_row_count"] == 4
        assert data["total_emits"] == 1
        assert data["test_fixture_emits"] == 3
        for tid in ("Test_Agent_S9", "test_agent", "test_stress"):
            assert tid not in data["by_agent_id"]

    @pytest.mark.asyncio
    async def test_include_events_payload(self):
        events = [self._fake_event("2026-05-19T10:00:00+00:00", "agent-1", caller_channel="rest")]

        async def fake_query(**kwargs):
            return events

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            result = await handle_audit_events({
                "event_type": "continuity_token_deprecated_accept",
                "since": "7d",
                "include_events": True,
            })

        data = parse_result(result)
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["details"]["caller_channel"] == "rest"

    @pytest.mark.asyncio
    async def test_iso_window_parsing(self):
        """ISO 8601 since/until pass through unchanged."""
        captured: Dict[str, Any] = {}

        async def fake_query(**kwargs):
            captured.update(kwargs)
            return []

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            await handle_audit_events({
                "event_type": "x",
                "since": "2026-04-24T00:00:00Z",
                "until": "2026-05-08T00:00:00Z",
            })

        from datetime import datetime
        assert datetime.fromisoformat(captured["start_time"]).isoformat().startswith("2026-04-24")
        assert datetime.fromisoformat(captured["end_time"]).isoformat().startswith("2026-05-08")

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        captured: Dict[str, Any] = {}

        async def fake_query(**kwargs):
            captured.update(kwargs)
            return []

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.observability.handlers import handle_audit_events
            await handle_audit_events({"event_type": "x", "limit": 99999})

        assert captured["limit"] == 5000

    @pytest.mark.asyncio
    async def test_routed_through_observe(self):
        """action='audit_events' routes through the unified observe tool."""
        async def fake_query(**kwargs):
            return [self._fake_event("2026-05-19T10:00:00+00:00", "agent-1")]

        with patch("src.audit_db.query_audit_events_async", new=fake_query):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({
                "action": "audit_events",
                "event_type": "continuity_token_deprecated_accept",
                "since": "14d",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["total_emits"] == 1
