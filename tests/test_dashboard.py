"""Tests for src/mcp_handlers/admin/dashboard.py — dashboard handler coverage.

Tests cover:
- Basic dashboard response structure
- Filtering by recent_days, min_updates, limit
- Pinned agent always-include behavior
- Datetime parsing failures (fail closed)
- EISV state merging from DB
- Sorting: pinned first, then by update count
- Empty state (no agents, no DB states)
- Matview fallback in DB layer
- energy field used instead of state_json
- Pagination: offset, has_more
- Filtering: basin_filter, risk_threshold
- Backward compatibility: new params default to current behavior
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db.base import AgentStateRecord
from tests.helpers import parse_result


def make_metadata(
    agent_id: str,
    status: str = "active",
    total_updates: int = 5,
    last_update: Optional[str] = None,
    tags: Optional[list] = None,
    label: Optional[str] = None,
):
    """Create a minimal AgentMetadata-like object."""
    if last_update is None:
        last_update = datetime.now(timezone.utc).isoformat()

    meta = MagicMock()
    meta.agent_id = agent_id
    meta.status = status
    meta.total_updates = total_updates
    meta.last_update = last_update
    meta.tags = tags
    meta.label = label or agent_id
    return meta


def make_state(
    agent_id: str,
    identity_id: int = 1,
    E: float = 0.7,
    I: float = 0.8,
    S: float = 0.3,
    V: float = 0.1,
    coherence: float = 0.9,
    regime: str = "nominal",
    risk: float = 0.1,
    verdict: str = "proceed",
) -> AgentStateRecord:
    return AgentStateRecord(
        state_id=identity_id,
        identity_id=identity_id,
        agent_id=agent_id,
        recorded_at=datetime.now(timezone.utc),
        energy=E,
        entropy=S,
        integrity=I,
        stability_index=0.5,
        void=V,
        regime=regime,
        coherence=coherence,
        state_json={"E": E, "risk_score": risk, "verdict": verdict},
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_all_latest_agent_states = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_server():
    server = MagicMock()
    server.agent_metadata = {}
    return server


# ============================================================================
# Basic response structure
# ============================================================================

class TestDashboardBasic:

    @pytest.mark.asyncio
    async def test_empty_dashboard(self, mock_db, mock_server):
        """No agents, no states — returns empty list."""
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["agents"] == []
        assert data["total"] == 0
        assert data["showing"] == 0

    @pytest.mark.asyncio
    async def test_single_agent_with_eisv(self, mock_db, mock_server):
        """One active agent with matching DB state."""
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("agent-1", E=0.75, I=0.85, S=0.2, V=0.05)
        ]
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1", total_updates=10),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["showing"] == 1
        agent = data["agents"][0]
        assert agent["id"] == "agent-1"
        assert agent["eisv"]["E"] == 0.75
        assert agent["eisv"]["I"] == 0.85
        assert agent["eisv"]["S"] == 0.2
        assert agent["eisv"]["V"] == 0.05

    @pytest.mark.asyncio
    async def test_agent_without_db_state(self, mock_db, mock_server):
        """Agent in metadata but no DB state — no eisv key."""
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["total"] == 1
        agent = data["agents"][0]
        assert "eisv" not in agent

    @pytest.mark.asyncio
    async def test_response_includes_offset_and_has_more(self, mock_db, mock_server):
        """Response always includes offset and has_more fields."""
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert "offset" in data
        assert "has_more" in data
        assert data["offset"] == 0
        assert data["has_more"] is False


# ============================================================================
# Filtering
# ============================================================================

class TestDashboardFiltering:

    @pytest.mark.asyncio
    async def test_filter_inactive_agents(self, mock_db, mock_server):
        """Agents with status != 'active' are excluded."""
        mock_server.agent_metadata = {
            "active-1": make_metadata("active-1", status="active"),
            "paused-1": make_metadata("paused-1", status="paused"),
            "archived-1": make_metadata("archived-1", status="archived"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "active-1"

    @pytest.mark.asyncio
    async def test_filter_by_min_updates(self, mock_db, mock_server):
        """Agents below min_updates threshold are excluded."""
        mock_server.agent_metadata = {
            "few": make_metadata("few", total_updates=2),
            "many": make_metadata("many", total_updates=10),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"min_updates": 5})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "many"

    @pytest.mark.asyncio
    async def test_filter_by_recent_days(self, mock_db, mock_server):
        """Agents outside recent_days window are excluded."""
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "recent": make_metadata("recent", last_update=now.isoformat()),
            "old": make_metadata("old", last_update=(now - timedelta(days=5)).isoformat()),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 1})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "recent"

    @pytest.mark.asyncio
    async def test_recent_days_zero_shows_all(self, mock_db, mock_server):
        """recent_days=0 disables recency filter."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        mock_server.agent_metadata = {
            "old": make_metadata("old", last_update=old_time),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 0})

        data = parse_result(result)
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_limit_caps_results(self, mock_db, mock_server):
        """Limit parameter caps returned agents."""
        mock_server.agent_metadata = {
            f"agent-{i}": make_metadata(f"agent-{i}", total_updates=10 - i)
            for i in range(5)
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"limit": 2})

        data = parse_result(result)
        assert data["total"] == 5
        assert data["showing"] == 2


# ============================================================================
# Pinned agents
# ============================================================================

class TestDashboardPinned:

    @pytest.mark.asyncio
    async def test_pinned_bypasses_recency(self, mock_db, mock_server):
        """Pinned agents are shown even if outside recency window."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        mock_server.agent_metadata = {
            "pinned-old": make_metadata("pinned-old", last_update=old_time, tags=["pinned"]),
            "normal-old": make_metadata("normal-old", last_update=old_time, tags=[]),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 1})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "pinned-old"
        assert data["agents"][0]["pinned"] is True

    @pytest.mark.asyncio
    async def test_pinned_sorted_first(self, mock_db, mock_server):
        """Pinned agents appear before non-pinned regardless of update count."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "busy": make_metadata("busy", total_updates=100, last_update=now),
            "pinned": make_metadata("pinned", total_updates=1, last_update=now, tags=["pinned"]),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["agents"][0]["id"] == "pinned"
        assert data["agents"][1]["id"] == "busy"


# ============================================================================
# Datetime parsing edge cases
# ============================================================================

class TestDashboardDatetime:

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_excluded(self, mock_db, mock_server):
        """Agents with unparseable last_update are excluded (fail closed)."""
        mock_server.agent_metadata = {
            "bad-ts": make_metadata("bad-ts", last_update="not-a-date"),
            "good-ts": make_metadata("good-ts"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 1})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "good-ts"

    @pytest.mark.asyncio
    async def test_z_suffix_timestamp(self, mock_db, mock_server):
        """Timestamps with Z suffix are parsed correctly."""
        now = datetime.now(timezone.utc)
        z_ts = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        mock_server.agent_metadata = {
            "z-agent": make_metadata("z-agent", last_update=z_ts),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 1})

        data = parse_result(result)
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_naive_timestamp_treated_as_utc(self, mock_db, mock_server):
        """Naive timestamps (no tz) treated as UTC."""
        now = datetime.now(timezone.utc)
        naive_ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f")  # no tz suffix
        mock_server.agent_metadata = {
            "naive": make_metadata("naive", last_update=naive_ts),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"recent_days": 1})

        data = parse_result(result)
        assert data["total"] == 1


# ============================================================================
# Sorting
# ============================================================================

class TestDashboardSorting:

    @pytest.mark.asyncio
    async def test_sort_by_update_count(self, mock_db, mock_server):
        """Non-pinned agents sorted by descending update count."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "few": make_metadata("few", total_updates=3, last_update=now),
            "many": make_metadata("many", total_updates=30, last_update=now),
            "mid": make_metadata("mid", total_updates=15, last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        ids = [a["id"] for a in data["agents"]]
        assert ids == ["many", "mid", "few"]


# ============================================================================
# EISV merge
# ============================================================================

class TestDashboardEISVMerge:

    @pytest.mark.asyncio
    async def test_eisv_values_rounded(self, mock_db, mock_server):
        """EISV values are rounded to 4 decimal places."""
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("agent-1", E=0.123456789, I=0.987654321, S=0.111111111, V=0.222222222)
        ]
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        eisv = data["agents"][0]["eisv"]
        assert eisv["E"] == 0.1235
        assert eisv["I"] == 0.9877
        assert eisv["S"] == 0.1111
        assert eisv["V"] == 0.2222

    @pytest.mark.asyncio
    async def test_eisv_none_values_preserved(self, mock_db, mock_server):
        """None EISV values come through as None."""
        state = make_state("agent-1")
        state.integrity = None
        state.entropy = None
        mock_db.get_all_latest_agent_states.return_value = [state]
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        eisv = data["agents"][0]["eisv"]
        assert eisv["I"] is None
        assert eisv["S"] is None

    @pytest.mark.asyncio
    async def test_multiple_agents_correct_state_mapping(self, mock_db, mock_server):
        """Each agent gets its own EISV state, not another agent's."""
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("agent-a", E=0.9, regime="nominal"),
            make_state("agent-b", E=0.1, regime="chaos"),
        ]
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "agent-a": make_metadata("agent-a", last_update=now),
            "agent-b": make_metadata("agent-b", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        by_id = {a["id"]: a for a in data["agents"]}
        assert by_id["agent-a"]["eisv"]["E"] == 0.9
        assert by_id["agent-a"]["eisv"]["basin"] == "nominal"
        assert by_id["agent-b"]["eisv"]["E"] == 0.1
        assert by_id["agent-b"]["eisv"]["basin"] == "chaos"

    @pytest.mark.asyncio
    async def test_energy_from_record_not_state_json(self, mock_db, mock_server):
        """Dashboard reads s.energy (the field), not state_json["E"]."""
        # Create state where energy field differs from state_json E
        state = AgentStateRecord(
            state_id=1,
            identity_id=1,
            agent_id="agent-1",
            recorded_at=datetime.now(timezone.utc),
            energy=0.42,
            entropy=0.3,
            integrity=0.8,
            stability_index=0.0,
            void=0.1,
            regime="nominal",
            coherence=0.9,
            state_json={"E": 0.99, "risk_score": 0.1, "verdict": "proceed"},
        )
        mock_db.get_all_latest_agent_states.return_value = [state]
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        # Should use s.energy (0.42), NOT state_json["E"] (0.99)
        assert data["agents"][0]["eisv"]["E"] == 0.42


# ============================================================================
# Error handling
# ============================================================================

class TestDashboardErrors:

    @pytest.mark.asyncio
    async def test_db_error_returns_error_response(self, mock_server):
        """Database failure returns error_response, not exception."""
        mock_db = AsyncMock()
        mock_db.get_all_latest_agent_states = AsyncMock(side_effect=RuntimeError("connection lost"))
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["success"] is False
        assert "connection lost" in data["error"]


# ============================================================================
# Inner deadline / degraded fall-through (critique #6: timeout coarseness)
# ============================================================================

class TestDashboardInnerDeadline:

    @pytest.mark.asyncio
    async def test_slow_db_degrades_to_in_memory(self, mock_server, monkeypatch):
        """A DB read that exceeds the inner budget degrades fast instead of
        hanging the full 15s decorator timeout, and still returns the in-memory
        overview with a degraded flag."""
        monkeypatch.setenv("UNITARES_DASHBOARD_DB_BUDGET_S", "0.05")

        async def _slow_states():
            await asyncio.sleep(5)  # would blow the inner budget
            return []

        mock_db = AsyncMock()
        mock_db.get_all_latest_agent_states = _slow_states
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1", total_updates=5, last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        # Success (not a hard error) and the in-memory agent is still surfaced.
        assert data["success"] is True
        assert data["degraded"] is True
        assert "degraded_reason" in data
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "agent-1"
        # No DB-derived EISV on the degraded call.
        assert "eisv" not in data["agents"][0]

    @pytest.mark.asyncio
    async def test_fast_db_not_degraded(self, mock_db, mock_server):
        """Normal (fast) DB read reports degraded=False."""
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1"),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["degraded"] is False
        assert "degraded_reason" not in data

    def test_budget_env_override_and_fallback(self, monkeypatch):
        from src.mcp_handlers.admin.dashboard import (
            _dashboard_db_budget_s,
            _DASHBOARD_DB_BUDGET_S_DEFAULT,
        )

        monkeypatch.delenv("UNITARES_DASHBOARD_DB_BUDGET_S", raising=False)
        assert _dashboard_db_budget_s() == _DASHBOARD_DB_BUDGET_S_DEFAULT

        monkeypatch.setenv("UNITARES_DASHBOARD_DB_BUDGET_S", "2.5")
        assert _dashboard_db_budget_s() == 2.5

        # Garbage / non-positive values fall back to the default.
        for bad in ("not-a-number", "0", "-3"):
            monkeypatch.setenv("UNITARES_DASHBOARD_DB_BUDGET_S", bad)
            assert _dashboard_db_budget_s() == _DASHBOARD_DB_BUDGET_S_DEFAULT


# ============================================================================
# Schema validation
# ============================================================================

class TestDashboardSchema:

    def test_default_values(self):
        from src.mcp_handlers.schemas.dashboard import DashboardParams
        params = DashboardParams()
        assert params.recent_days == 1
        assert params.min_updates == 1
        assert params.limit == 15
        assert params.offset == 0
        assert params.basin_filter is None
        assert params.risk_threshold is None

    def test_limit_bounds(self):
        from src.mcp_handlers.schemas.dashboard import DashboardParams
        from pydantic import ValidationError

        # Min bound
        with pytest.raises(ValidationError):
            DashboardParams(limit=0)

        # Max bound
        with pytest.raises(ValidationError):
            DashboardParams(limit=101)

        # Valid edges
        assert DashboardParams(limit=1).limit == 1
        assert DashboardParams(limit=100).limit == 100

    def test_offset_bounds(self):
        from src.mcp_handlers.schemas.dashboard import DashboardParams
        from pydantic import ValidationError

        # Negative offset rejected
        with pytest.raises(ValidationError):
            DashboardParams(offset=-1)

        # Zero and positive are fine
        assert DashboardParams(offset=0).offset == 0
        assert DashboardParams(offset=50).offset == 50

    def test_risk_threshold_bounds(self):
        from src.mcp_handlers.schemas.dashboard import DashboardParams
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DashboardParams(risk_threshold=-0.1)
        with pytest.raises(ValidationError):
            DashboardParams(risk_threshold=1.1)

        assert DashboardParams(risk_threshold=0.0).risk_threshold == 0.0
        assert DashboardParams(risk_threshold=1.0).risk_threshold == 1.0
        assert DashboardParams(risk_threshold=0.5).risk_threshold == 0.5

    def test_basin_filter_accepts_string(self):
        from src.mcp_handlers.schemas.dashboard import DashboardParams
        params = DashboardParams(basin_filter="critical")
        assert params.basin_filter == "critical"


# ============================================================================
# Pagination (offset + has_more)
# ============================================================================

class TestDashboardPagination:

    @pytest.mark.asyncio
    async def test_offset_skips_agents(self, mock_db, mock_server):
        """offset=2 skips the first 2 agents."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            f"agent-{i}": make_metadata(f"agent-{i}", total_updates=10 - i, last_update=now)
            for i in range(5)
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"offset": 2, "limit": 15})

        data = parse_result(result)
        assert data["total"] == 5
        assert data["showing"] == 3
        assert data["offset"] == 2

    @pytest.mark.asyncio
    async def test_has_more_true_when_more_exist(self, mock_db, mock_server):
        """has_more is True when offset+showing < total."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            f"agent-{i}": make_metadata(f"agent-{i}", total_updates=10 - i, last_update=now)
            for i in range(5)
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"limit": 2, "offset": 0})

        data = parse_result(result)
        assert data["has_more"] is True
        assert data["showing"] == 2
        assert data["total"] == 5

    @pytest.mark.asyncio
    async def test_has_more_false_at_end(self, mock_db, mock_server):
        """has_more is False when all remaining agents are shown."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            f"agent-{i}": make_metadata(f"agent-{i}", total_updates=10 - i, last_update=now)
            for i in range(3)
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"limit": 15, "offset": 0})

        data = parse_result(result)
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_offset_beyond_total(self, mock_db, mock_server):
        """offset beyond total returns empty agents list."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"offset": 100})

        data = parse_result(result)
        assert data["agents"] == []
        assert data["total"] == 1
        assert data["showing"] == 0
        assert data["has_more"] is False


# ============================================================================
# Basin filter + risk threshold
# ============================================================================

class TestDashboardAdvancedFiltering:

    @pytest.mark.asyncio
    async def test_basin_filter(self, mock_db, mock_server):
        """basin_filter only returns agents matching that regime."""
        now = datetime.now(timezone.utc).isoformat()
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("nominal-1", regime="nominal"),
            make_state("critical-1", regime="critical"),
            make_state("nominal-2", regime="nominal"),
        ]
        mock_server.agent_metadata = {
            "nominal-1": make_metadata("nominal-1", last_update=now),
            "critical-1": make_metadata("critical-1", last_update=now),
            "nominal-2": make_metadata("nominal-2", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"basin_filter": "critical"})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "critical-1"

    @pytest.mark.asyncio
    async def test_basin_filter_excludes_agents_without_eisv(self, mock_db, mock_server):
        """Agents without EISV data are excluded by basin_filter."""
        now = datetime.now(timezone.utc).isoformat()
        mock_server.agent_metadata = {
            "no-state": make_metadata("no-state", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"basin_filter": "nominal"})

        data = parse_result(result)
        assert data["total"] == 0
        assert data["agents"] == []

    @pytest.mark.asyncio
    async def test_risk_threshold(self, mock_db, mock_server):
        """risk_threshold only returns agents at or above the threshold."""
        now = datetime.now(timezone.utc).isoformat()
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("low-risk", risk=0.1),
            make_state("high-risk", risk=0.8),
            make_state("mid-risk", risk=0.5),
        ]
        mock_server.agent_metadata = {
            "low-risk": make_metadata("low-risk", last_update=now),
            "high-risk": make_metadata("high-risk", last_update=now),
            "mid-risk": make_metadata("mid-risk", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"risk_threshold": 0.5})

        data = parse_result(result)
        assert data["total"] == 2
        ids = {a["id"] for a in data["agents"]}
        assert ids == {"high-risk", "mid-risk"}

    @pytest.mark.asyncio
    async def test_combined_filters(self, mock_db, mock_server):
        """basin_filter + risk_threshold work together."""
        now = datetime.now(timezone.utc).isoformat()
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("crit-high", regime="critical", risk=0.9),
            make_state("crit-low", regime="critical", risk=0.1),
            make_state("nom-high", regime="nominal", risk=0.9),
        ]
        mock_server.agent_metadata = {
            "crit-high": make_metadata("crit-high", last_update=now),
            "crit-low": make_metadata("crit-low", last_update=now),
            "nom-high": make_metadata("nom-high", last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({"basin_filter": "critical", "risk_threshold": 0.5})

        data = parse_result(result)
        assert data["total"] == 1
        assert data["agents"][0]["id"] == "crit-high"


# ============================================================================
# Backward compatibility
# ============================================================================

class TestDashboardBackwardCompat:

    @pytest.mark.asyncio
    async def test_defaults_match_previous_behavior(self, mock_db, mock_server):
        """With no new params, behavior is identical to pre-change."""
        now = datetime.now(timezone.utc).isoformat()
        mock_db.get_all_latest_agent_states.return_value = [
            make_state("agent-1", E=0.7, I=0.8, S=0.3, V=0.1),
        ]
        mock_server.agent_metadata = {
            "agent-1": make_metadata("agent-1", total_updates=5, last_update=now),
        }
        with patch("src.mcp_handlers.admin.dashboard.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.admin.dashboard.mcp_server", mock_server):
            from src.mcp_handlers.admin.dashboard import handle_dashboard
            result = await handle_dashboard({})

        data = parse_result(result)
        # All original fields present
        assert "agents" in data
        assert "total" in data
        assert "showing" in data
        # New fields also present but with default values
        assert data["offset"] == 0
        assert data["has_more"] is False
        # EISV data works as before
        agent = data["agents"][0]
        assert agent["eisv"]["E"] == 0.7
