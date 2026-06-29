"""Tests for the DB-backed tool-usage reader (audit.tool_usage).

The legacy JSONL sink is best-effort and drifted stale; the live sink is the
audit.tool_usage table. These tests cover the reader's shape parity with the
JSONL reader, REMOVED_TOOLS filtering, the DB-unavailable fallback path, and the
progressive-ordering key fix (call_count -> total_calls).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers.core  # noqa: F401  (settle handler registration)
import src.mcp_handlers.consolidated  # noqa: F401

from src.audit_db import get_tool_usage_stats_async
from src.mcp_handlers.introspection.tool_introspection import handle_list_tools


def _mock_db(rows):
    """A get_db() stand-in whose acquire() yields a conn returning ``rows``."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db._pool = object()  # truthy so the reader skips db.init()
    db.acquire = MagicMock(return_value=cm)
    return db, conn


def test_db_reader_shape_filtering_and_sorting():
    rows = [
        {"tool_name": "get_governance_metrics", "total_calls": 100, "success_count": 98},
        {"tool_name": "knowledge", "total_calls": 10, "success_count": 9},
        # REMOVED_TOOLS entry must be dropped:
        {"tool_name": "store_knowledge", "total_calls": 999, "success_count": 999},
    ]
    db, _conn = _mock_db(rows)
    with patch("src.db.get_db", return_value=db):
        stats = asyncio.run(get_tool_usage_stats_async(window_hours=24))

    assert stats is not None
    assert stats["source"] == "db"
    assert "store_knowledge" not in stats["tools"], "REMOVED_TOOLS not filtered"
    assert stats["unique_tools"] == 2
    assert stats["total_calls"] == 110  # removed tool excluded from total
    # most_used sorted by calls desc
    assert stats["most_used"][0] == {"tool": "get_governance_metrics", "calls": 100}
    # per-tool shape parity with the JSONL reader
    t = stats["tools"]["knowledge"]
    assert t["total_calls"] == 10
    assert t["success_count"] == 9
    assert t["error_count"] == 1
    assert t["success_rate"] == pytest.approx(0.9)
    assert t["percentage_of_total"] == pytest.approx(10 / 110 * 100)


def test_db_reader_agent_usage_only_with_agent_filter():
    rows = [{"tool_name": "identity", "total_calls": 4, "success_count": 4}]
    db, _conn = _mock_db(rows)
    with patch("src.db.get_db", return_value=db):
        no_filter = asyncio.run(get_tool_usage_stats_async(window_hours=24))
        with_filter = asyncio.run(
            get_tool_usage_stats_async(window_hours=24, agent_id="agent-x")
        )
    assert no_filter["agent_usage"] is None
    assert with_filter["agent_usage"] == {"agent-x": {"identity": 4}}


def test_db_reader_returns_none_when_db_unavailable():
    # get_db raising must yield None so callers fall back to JSONL — never raise.
    with patch("src.db.get_db", side_effect=RuntimeError("no pool")):
        stats = asyncio.run(get_tool_usage_stats_async(window_hours=24))
    assert stats is None


def test_get_tool_usage_stats_handler_falls_back_to_jsonl():
    from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats

    tracker = MagicMock()
    tracker.get_usage_stats = MagicMock(return_value={"total_calls": 7, "tools": {}})
    with ExitStack() as stack:
        # DB path unavailable -> None
        stack.enter_context(
            patch("src.audit_db.get_tool_usage_stats_async", new=AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=tracker)
        )
        result = asyncio.run(handle_get_tool_usage_stats({}))
    payload = json.loads(result[0].text)
    body = payload.get("data", payload)
    assert body.get("source") == "jsonl_fallback"
    assert body.get("total_calls") == 7


def test_progressive_ordering_uses_total_calls(monkeypatch):
    """Regression: order/grouping read per-tool 'total_calls' (not the nonexistent
    'call_count', which made progressive ordering a silent no-op)."""
    import src.mcp_handlers.introspection.tool_introspection as ti

    async def fake_usage(window_hours: int = 168):
        return {"health_check": {"total_calls": 50}, "describe_tool": {"total_calls": 5}}

    monkeypatch.setattr(ti, "_usage_tools_for_ordering", fake_usage)
    resp = json.loads(asyncio.run(handle_list_tools({"progressive": True}))[0].text)
    names = [t["name"] for t in resp["tools"]]
    # 50-call tool must sort ahead of the 5-call tool; both ahead of any 0-call tool.
    assert names.index("health_check") < names.index("describe_tool")
