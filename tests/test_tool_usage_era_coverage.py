"""`audit.tool_usage` is two eras in one table; a reader must say which it read.

Until PR #1424 deployed (2026-07-31), the FastMCP wrapper serving `/mcp` and
`/sse` recorded nothing — only REST callers landed rows. Every count, adoption
figure and per-agent attribution taken over a window reaching before that
instant measured hooks and pollers rather than agents, and any trend line
crossing it measures the instrumentation change.

Nothing in the schema, the tool, or the dashboard says so. The safeguard has
been an operator remembering — which is not a safeguard, it is a near miss
waiting to be published. These tests pin the disclosure so a stats reader
cannot quietly hand back a number whose window it cannot support.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.audit_db import (
    TOOL_USAGE_MCP_INSTRUMENTED_SINCE,
    tool_usage_window_coverage,
)


def test_boundary_is_utc_aware():
    """A naive bound would compare wrong against UTC row timestamps.

    Same class as the audit-partition bug where Denver-local bounds against
    `current_date` opened a six-hour hole on the first of the month.
    """
    assert TOOL_USAGE_MCP_INSTRUMENTED_SINCE.tzinfo is not None
    assert TOOL_USAGE_MCP_INSTRUMENTED_SINCE.utcoffset() == timedelta(0)


def test_window_inside_the_instrumented_era_carries_no_caveat():
    """Don't attach a warning to a number that does not need one.

    Presence of `coverage` is the signal, so emitting it always would make it
    unreadable.
    """
    now = TOOL_USAGE_MCP_INSTRUMENTED_SINCE + timedelta(days=30)
    assert tool_usage_window_coverage(24, now=now) is None
    assert tool_usage_window_coverage(24 * 29, now=now) is None


def test_window_crossing_the_boundary_is_flagged():
    now = TOOL_USAGE_MCP_INSTRUMENTED_SINCE + timedelta(days=5)
    coverage = tool_usage_window_coverage(24 * 30, now=now)

    assert coverage is not None
    assert coverage["partial"] is True
    assert coverage["mcp_instrumented_since"] == (
        TOOL_USAGE_MCP_INSTRUMENTED_SINCE.isoformat()
    )
    # The caveat has to name what is actually missing, not just say "partial".
    caveat = coverage["caveat"].lower()
    assert "rest" in caveat
    assert "/mcp" in caveat


def test_boundary_is_exclusive_at_the_edge():
    """A window starting exactly at the boundary is fully instrumented."""
    now = TOOL_USAGE_MCP_INSTRUMENTED_SINCE + timedelta(hours=10)
    assert tool_usage_window_coverage(10, now=now) is None
    # One hour earlier reaches into the dark era.
    assert tool_usage_window_coverage(11, now=now) is not None


@pytest.mark.parametrize("window_hours", [24 * 30, 24 * 60, 24 * 90])
def test_wide_windows_are_flagged_while_the_boundary_is_recent(window_hours):
    """The realistic failure: a 30/60/90-day lookback run soon after the fix.

    This is the shape of the near miss — a 60-day query run 2026-08-11 returning
    numbers that read as agent behaviour and were not.
    """
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    assert tool_usage_window_coverage(window_hours, now=now) is not None


def test_the_default_seven_day_window_is_clean_by_2026_08_11():
    """Not everything wide is dirty — the caveat must stay rare to stay read.

    The reader's own default is 7 days; eleven days past the boundary that
    window is entirely inside the instrumented era, and flagging it anyway
    would train callers to ignore the field.
    """
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    assert tool_usage_window_coverage(24 * 7, now=now) is None


def test_jsonl_fallback_declares_a_different_gap_than_the_db_path():
    """The two sinks are unreliable in different ways; one caveat would lie.

    `audit.tool_usage` has two eras and gained the MCP transport at a known
    instant. The JSONL sink never receives that transport at all — #1424
    instrumented the MCP wrapper with `audit_only=True` deliberately, because
    that sink feeds `compute_behavioral_sensor_eisv` and adding a transport to
    it would be a fleet-wide sensor change. So the fallback is permanently
    REST+stdio, at every window, and must not borrow the era wording.
    """
    import asyncio
    import json
    from unittest.mock import patch

    from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats

    with patch("src.audit_db.get_tool_usage_stats_async", return_value=None), \
         patch("src.tool_usage_tracker.get_tool_usage_tracker") as tracker:
        tracker.return_value.get_usage_stats.return_value = {"total_calls": 0}
        result = asyncio.run(handle_get_tool_usage_stats({"window_hours": 1}))

    payload = json.loads(result[0].text)
    assert payload["source"] == "jsonl_fallback"
    coverage = payload["coverage"]
    # A one-hour window cannot cross the era boundary, so this caveat is
    # necessarily about the sink, not the window.
    assert coverage["transports"] == ["rest", "stdio"]
    assert "mcp_instrumented_since" not in coverage
    assert "missing here" in coverage["caveat"]


def test_stats_reader_attaches_coverage_only_when_partial(monkeypatch):
    """The disclosure must ride the payload, not live in a docstring."""
    import asyncio

    import src.audit_db as audit_db

    captured = {}

    def _fake_coverage(window_hours, *, now=None):
        captured["window_hours"] = window_hours
        return {"partial": True, "caveat": "x"} if window_hours > 100 else None

    monkeypatch.setattr(audit_db, "tool_usage_window_coverage", _fake_coverage)

    class _Conn:
        async def fetch(self, *a, **k):
            return []

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _DB:
        _pool = object()

        def acquire(self):
            return _Acquire()

    monkeypatch.setattr("src.db.get_db", lambda: _DB())

    wide = asyncio.run(audit_db.get_tool_usage_stats_async(window_hours=24 * 30))
    assert wide is not None and "coverage" in wide
    assert captured["window_hours"] == 24 * 30

    narrow = asyncio.run(audit_db.get_tool_usage_stats_async(window_hours=24))
    assert narrow is not None and "coverage" not in narrow
