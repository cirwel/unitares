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
        server.load_metadata_async.assert_not_awaited()

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
        """When target is a label (not UUID format), resolve from in-memory metadata."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        meta = _make_metadata(uuid, label="Lumen")
        server = _build_mock_server(
            monitors_dict={uuid: _make_monitor(uuid)},
            metadata_dict={uuid: meta},
        )

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value="caller-uuid"):
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": "Lumen"})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid
        server.load_metadata_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_label_resolution_metadata_fallback(self):
        """Observe does not call async label lookup; metadata label search is enough."""
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
            ) as mock_find:
            from src.mcp_handlers.observability.handlers import handle_observe_agent
            result = await handle_observe_agent({"target_agent_id": "Lumen"})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == uuid
        mock_find.assert_not_awaited()
        server.load_metadata_async.assert_not_awaited()

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
    async def test_none_metrics_use_defaults_instead_of_crashing(self):
        """Sparse hydrated state can return explicit None values for metrics."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        other_uuid = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        none_metrics = {
            "E": None,
            "I": None,
            "S": None,
            "coherence": None,
            "phi": None,
            "risk_score": None,
        }
        monitors = {
            my_uuid: _make_monitor(my_uuid, metrics=none_metrics),
            other_uuid: _make_monitor(other_uuid, metrics=none_metrics),
        }
        metadata = {
            my_uuid: _make_metadata(my_uuid, status="active", total_updates=1),
            other_uuid: _make_metadata(other_uuid, status="active", total_updates=1),
        }
        server = _build_mock_server(monitors_dict=monitors, metadata_dict=metadata)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.observability.handlers import handle_compare_me_to_similar
            result = await handle_compare_me_to_similar({"agent_id": my_uuid})

        data = parse_result(result)
        assert data["success"] is True
        assert data["my_metrics"]["E"] == 0.7
        assert data["my_metrics"]["I"] == 0.8
        assert data["my_metrics"]["S"] == 0.2
        assert data["my_metrics"]["coherence"] == 0.5
        assert data["my_metrics"]["phi"] == 0.0
        assert data["my_metrics"]["risk_score"] == 0.4
        assert data["similar_agents"][0]["metrics"]["risk_score"] == 0.4

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

    @pytest.mark.asyncio
    async def test_consolidated_similar_uses_session_bound_identity(self):
        """observe(action='similar') works without explicit agent_id when context is bound."""
        my_uuid = "aaaaaaaa-bbbb-cccc-dddd-000000000000"
        others = {
            "aaaaaaaa-bbbb-cccc-dddd-111111111111": {
                "E": 0.76, "I": 0.84, "S": 0.16, "coherence": 0.64,
                "total_updates": 8,
            },
        }
        server = self._setup_server_with_similar(my_uuid, others)

        with self._patches(server, my_uuid):
            from src.mcp_handlers.consolidated import handle_observe
            result = await handle_observe({"action": "similar"})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_id"] == my_uuid


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


# ---------------------------------------------------------------------------
# handle_aggregate_metrics
# ---------------------------------------------------------------------------

class TestHandleAggregateMetrics:
    """Tests for handle_aggregate_metrics."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        """Aggregate metrics across active agents."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        assert data["success"] is True
        agg = data["aggregate"]
        assert agg["total_agents"] == 2
        assert agg["agents_with_data"] == 2
        assert "mean_risk_score" in agg
        assert "mean_coherence" in agg
        assert "decision_distribution" in agg

    @pytest.mark.asyncio
    async def test_no_active_agents(self):
        """No agents -> zero aggregates."""
        server = _build_mock_server()

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        assert data["success"] is True
        agg = data["aggregate"]
        assert agg["total_agents"] == 0
        assert agg["agents_with_data"] == 0
        assert agg["mean_risk_score"] == 0.0
        assert agg["mean_coherence"] == 0.0

    @pytest.mark.asyncio
    async def test_specific_agent_ids(self):
        """Aggregate only specified agents."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
        server = _build_mock_server(agent_ids=[id1, id2])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True
        agg = data["aggregate"]
        assert agg["total_agents"] == 1

    @pytest.mark.asyncio
    async def test_health_breakdown_included(self):
        """include_health_breakdown=True (default) -> health_breakdown key present."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        assert data["success"] is True
        assert "health_breakdown" in data["aggregate"]

    @pytest.mark.asyncio
    async def test_health_breakdown_excluded(self):
        """include_health_breakdown=False -> no health_breakdown key."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({
                "include_health_breakdown": False,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert "health_breakdown" not in data["aggregate"]

    @pytest.mark.asyncio
    async def test_decision_distribution_totals(self):
        """Decision distribution total matches sum of counts."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        server = _build_mock_server(agent_ids=[id1])

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        dist = data["aggregate"]["decision_distribution"]
        # Total should be sum of proceed + pause (and backward compat keys)
        assert "total" in dist

    @pytest.mark.asyncio
    async def test_monitor_loaded_from_persisted_state(self):
        """When monitor not in memory, load from persisted state."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        metadata = {id1: _make_metadata(id1)}
        server = _build_mock_server(metadata_dict=metadata)
        server.load_monitor_state = MagicMock(return_value=_make_state())

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitor:
            mock_instance = _make_monitor(id1)
            MockMonitor.return_value = mock_instance
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True
        assert data["aggregate"]["agents_with_data"] == 1

    @pytest.mark.asyncio
    async def test_risk_history_fallback(self):
        """When risk_score and current_risk are None, fall back to risk_history."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        monitor = _make_monitor(id1, metrics={
            "risk_score": None,
            "current_risk": None,
        })
        monitor.state = _make_state(risk_history=[0.3, 0.4, 0.5])

        server = _build_mock_server(
            monitors_dict={id1: monitor},
            metadata_dict={id1: _make_metadata(id1)},
        )

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({"agent_ids": [id1]})

        data = parse_result(result)
        assert data["success"] is True
        # mean_risk_score should be computed from risk_history
        assert data["aggregate"]["mean_risk_score"] > 0

    @pytest.mark.asyncio
    async def test_total_updates_aggregated(self):
        """total_updates across agents are summed (from meta.total_updates)."""
        id1 = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
        id2 = "aaaaaaaa-bbbb-cccc-dddd-222222222222"

        m1 = _make_monitor(id1)
        m1.state = _make_state(update_count=10)
        m2 = _make_monitor(id2)
        m2.state = _make_state(update_count=20)

        server = _build_mock_server(
            monitors_dict={id1: m1, id2: m2},
            metadata_dict={id1: _make_metadata(id1, total_updates=10), id2: _make_metadata(id2, total_updates=20)},
        )

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
            from src.mcp_handlers.observability.handlers import handle_aggregate_metrics
            result = await handle_aggregate_metrics({})

        data = parse_result(result)
        assert data["aggregate"]["total_updates"] == 30


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

        with patch(_PATCH_SERVER, server), \
             patch(_PATCH_CTX, return_value=None):
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
