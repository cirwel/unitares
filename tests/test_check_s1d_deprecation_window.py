"""Tests for the S1-d deprecation-window checker (scripts/dev)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from scripts.dev.check_s1d_deprecation_window import scan_audit_window, ACCEPT_EVENT

NOW = datetime(2026, 6, 24, 12, 0, 0)


def _accept(ts: datetime) -> str:
    return json.dumps({"event_type": ACCEPT_EVENT, "timestamp": ts.isoformat(), "details": {}})


def _other(ts: datetime, et: str = "mirror_signal.emit") -> str:
    return json.dumps({"event_type": et, "timestamp": ts.isoformat()})


def test_clean_window_with_marker_is_safe():
    audit = [_other(NOW - timedelta(days=1)), _other(NOW - timedelta(hours=2))]
    res = scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["...[S1-d] reached..."])
    assert res["verdict"] == "CLEAN"
    assert res["safe_to_retire"] is True
    assert res["deprecated_accept_in_window"] == 0
    assert res["surface_reached"] is True


def test_zero_accepts_without_marker_is_unconfirmed():
    audit = [_other(NOW - timedelta(days=1))]
    # No marker_lines provided → cannot confirm the surface was exercised.
    res = scan_audit_window(audit, window_days=14, now=NOW)
    assert res["verdict"] == "UNCONFIRMED"
    assert res["safe_to_retire"] is False
    assert res["surface_reached"] is None
    # Marker file present but lacking the token → still unconfirmed.
    res2 = scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["unrelated log line"])
    assert res2["verdict"] == "UNCONFIRMED"
    assert res2["surface_reached"] is False


def test_accept_in_window_is_dirty():
    audit = [_accept(NOW - timedelta(days=2)), _other(NOW)]
    res = scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["[S1-d]"])
    assert res["verdict"] == "DIRTY"
    assert res["safe_to_retire"] is False
    assert res["deprecated_accept_in_window"] == 1
    assert len(res["samples"]) == 1


def test_accept_outside_window_is_ignored():
    audit = [_accept(NOW - timedelta(days=40))]  # older than the 14d window
    res = scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["[S1-d]"])
    assert res["deprecated_accept_in_window"] == 0
    assert res["verdict"] == "CLEAN"


def test_malformed_and_blank_lines_tolerated():
    audit = ["", "  ", "not json at all", "{bad json", _accept(NOW - timedelta(days=1))]
    res = scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["[S1-d]"])
    # Only the one valid in-window accept counts; junk lines don't crash.
    assert res["deprecated_accept_in_window"] == 1
    assert res["verdict"] == "DIRTY"


def test_window_size_respected():
    audit = [_accept(NOW - timedelta(days=10))]
    # 7-day window excludes a 10-day-old accept.
    assert scan_audit_window(audit, window_days=7, now=NOW, marker_lines=["[S1-d]"])["verdict"] == "CLEAN"
    # 14-day window includes it.
    assert scan_audit_window(audit, window_days=14, now=NOW, marker_lines=["[S1-d]"])["verdict"] == "DIRTY"
