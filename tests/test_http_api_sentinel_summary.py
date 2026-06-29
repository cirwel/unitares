"""Tests for _sentinel_summary_from_events — the pure aggregator behind
/v1/sentinel/summary. Mirrors the test pattern in test_http_api_watcher_summary:
feed dict rows, assert on the shape returned, no HTTP/DB plumbing.

Sentinel findings are transient fleet-state signals (a coordinated_degradation
detected at 14:02 may already be gone at 14:07). There's no open/closed
lifecycle — operators want a chronological stream + counts by violation class
to spot which concerns are dominating right now."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.http_api import _sentinel_event_from_audit, _sentinel_summary_from_events


NOW = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


def _event(**kwargs):
    base = {
        "type": "sentinel_finding",
        "severity": "high",
        "message": "coordinated EISV degradation across 3 agents",
        "agent_id": "sentinel-uuid",
        "agent_name": "Sentinel",
        "violation_class": "coordinated_degradation",
        "finding_type": "cross_agent",
        "timestamp": NOW.isoformat(),
        "event_id": 1,
    }
    base.update(kwargs)
    return base


class TestCounts:
    def test_empty_input_returns_zeroed_shape(self):
        out = _sentinel_summary_from_events([], now=NOW, window_hours=24)
        assert out["total"] == 0
        assert out["by_severity"] == {}
        assert out["by_violation_class"] == []
        assert out["recent"] == []
        assert out["window_hours"] == 24

    def test_total_counts_all_findings(self):
        events = [_event(event_id=i) for i in range(5)]
        out = _sentinel_summary_from_events(events, now=NOW)
        assert out["total"] == 5

    def test_by_severity_counts_all_not_just_open(self):
        """Unlike Watcher, Sentinel findings don't have open/closed state —
        every severity count is surfaced so 'what's Sentinel seeing right
        now' is the honest answer."""
        events = [
            _event(event_id=1, severity="critical"),
            _event(event_id=2, severity="critical"),
            _event(event_id=3, severity="high"),
            _event(event_id=4, severity="medium"),
        ]
        out = _sentinel_summary_from_events(events, now=NOW)
        assert out["by_severity"] == {"critical": 2, "high": 1, "medium": 1}


class TestViolationClassBreakdown:
    def test_groups_by_violation_class_with_severity_subcounts(self):
        """The breakdown lets operators see which violation types dominate
        and whether each class is mostly high-severity or noise."""
        events = [
            _event(event_id=1, violation_class="coordinated_degradation", severity="high"),
            _event(event_id=2, violation_class="coordinated_degradation", severity="high"),
            _event(event_id=3, violation_class="coordinated_degradation", severity="medium"),
            _event(event_id=4, violation_class="identity_drift", severity="critical"),
        ]
        out = _sentinel_summary_from_events(events, now=NOW)
        by_class = {c["violation_class"]: c for c in out["by_violation_class"]}
        assert by_class["coordinated_degradation"]["count"] == 3
        assert by_class["coordinated_degradation"]["by_severity"] == {"high": 2, "medium": 1}
        assert by_class["identity_drift"]["count"] == 1
        assert by_class["identity_drift"]["by_severity"] == {"critical": 1}

    def test_breakdown_sorted_by_count_desc(self):
        events = (
            [_event(event_id=i, violation_class="high_vol") for i in range(4)]
            + [_event(event_id=10, violation_class="low_vol")]
        )
        out = _sentinel_summary_from_events(events, now=NOW)
        classes = [c["violation_class"] for c in out["by_violation_class"]]
        assert classes == ["high_vol", "low_vol"]

    def test_missing_violation_class_falls_under_question_mark(self):
        """Legacy Sentinel emissions without violation_class shouldn't crash
        the aggregator or silently disappear — they bucket under '?' so the
        operator sees they exist."""
        out = _sentinel_summary_from_events(
            [_event(violation_class=None)], now=NOW
        )
        assert out["by_violation_class"][0]["violation_class"] == "?"
        assert out["by_violation_class"][0]["count"] == 1


class TestRecentStream:
    def test_recent_is_newest_first(self):
        events = [
            _event(event_id=1, message="oldest",
                   timestamp=(NOW - timedelta(minutes=30)).isoformat()),
            _event(event_id=2, message="middle",
                   timestamp=(NOW - timedelta(minutes=15)).isoformat()),
            _event(event_id=3, message="newest",
                   timestamp=NOW.isoformat()),
        ]
        out = _sentinel_summary_from_events(events, now=NOW)
        messages = [r["message"] for r in out["recent"]]
        assert messages == ["newest", "middle", "oldest"]

    def test_recent_respects_limit(self):
        events = [
            _event(event_id=i, timestamp=(NOW - timedelta(minutes=i)).isoformat())
            for i in range(100)
        ]
        out = _sentinel_summary_from_events(events, now=NOW, recent_limit=10)
        assert len(out["recent"]) == 10

    def test_recent_includes_fields_the_panel_renders(self):
        """The panel renders timestamp, severity, violation_class, and message.
        Locking the projection here means future changes to the internal event
        shape can't silently break the UI."""
        events = [_event(event_id=1)]
        out = _sentinel_summary_from_events(events, now=NOW)
        row = out["recent"][0]
        for key in ("timestamp", "severity", "violation_class", "message"):
            assert key in row, f"panel needs {key}"


class TestWindow:
    def test_events_outside_window_excluded_from_totals(self):
        """A 3-day-old finding shouldn't contribute to a 24h 'right now' panel —
        that's the whole point of the window. If you want historical view,
        build a different panel."""
        events = [
            _event(event_id=1, timestamp=(NOW - timedelta(hours=2)).isoformat()),
            _event(event_id=2, timestamp=(NOW - timedelta(hours=2)).isoformat()),
            _event(event_id=3, timestamp=(NOW - timedelta(days=3)).isoformat()),
        ]
        out = _sentinel_summary_from_events(events, now=NOW, window_hours=24)
        assert out["total"] == 2

    def test_default_window_is_24h(self):
        out = _sentinel_summary_from_events([], now=NOW)
        assert out["window_hours"] == 24


class TestRobustness:
    def test_trailing_Z_timestamp_parses(self):
        events = [_event(timestamp="2026-04-24T11:30:00Z")]
        out = _sentinel_summary_from_events(events, now=NOW, window_hours=24)
        assert out["total"] == 1

    def test_malformed_timestamp_does_not_crash(self):
        """A bad timestamp counts toward totals but is omitted from the window
        check — dropping it entirely would hide findings from operators;
        crashing would hide the whole panel."""
        events = [_event(timestamp="not-a-date")]
        out = _sentinel_summary_from_events(events, now=NOW)
        # counted in totals, message still in recent
        assert out["total"] == 1
        assert len(out["recent"]) == 1

    def test_missing_severity_falls_under_question_mark(self):
        out = _sentinel_summary_from_events([_event(severity=None)], now=NOW)
        assert out["by_severity"] == {"?": 1}


class TestAlarmEventsSurface:
    """Forced-release alarm follow-up to PR #398 (2026-05-06).

    Pre-fix: alarms posted with `type="sentinel_forced_release_alarm"` were
    400'd by the /api/findings suffix gate, so the dashboard never saw them.
    PR #398 renamed the type to `sentinel_alarm_finding` so it satisfies the
    `_finding` suffix. This test class pins the aggregator's contract so
    alarms ride into the panel along with fleet-analysis findings — without
    this, fixing the suffix gate alone leaves alarms invisible.
    """

    def _alarm(self, **kwargs):
        base = {
            "type": "sentinel_alarm_finding",
            "severity": "high",
            "message": "forced release: dialectic:/x (lease lease-1)",
            "agent_id": "sentinel-uuid",
            "agent_name": "Sentinel",
            "fingerprint": "forced_release:ad_hoc:event-1",
            "alarm_kind": "ad_hoc",
            "timestamp": NOW.isoformat(),
            "event_id": 100,
        }
        base.update(kwargs)
        return base

    def test_alarms_count_toward_total(self):
        """Mixed stream: 2 fleet findings + 1 alarm → total 3."""
        events = [
            _event(event_id=1),
            _event(event_id=2),
            self._alarm(event_id=3),
        ]
        out = _sentinel_summary_from_events(events, now=NOW)
        assert out["total"] == 3, (
            "alarm events must count toward the panel's total — otherwise "
            "the suffix-gate fix leaves them silently invisible"
        )

    def test_alarms_appear_in_recent_stream(self):
        out = _sentinel_summary_from_events([self._alarm()], now=NOW)
        assert len(out["recent"]) == 1
        row = out["recent"][0]
        assert row["message"] == "forced release: dialectic:/x (lease lease-1)"
        assert row["severity"] == "high"

    def test_alarm_finding_type_falls_back_to_alarm_kind(self):
        """Alarms don't carry `finding_type` (only `alarm_kind`), so the
        recent-stream `finding_type` must fall back to alarm_kind. Otherwise
        the dashboard finding_type column shows null for every alarm row.
        """
        out = _sentinel_summary_from_events(
            [self._alarm(alarm_kind="conflict_batch")],
            now=NOW,
        )
        assert out["recent"][0]["finding_type"] == "conflict_batch"

    def test_explicit_finding_type_wins_over_alarm_kind(self):
        """If a future producer sets both fields, finding_type wins — we
        only fall back when finding_type is absent. Pinning this stops a
        later refactor from silently swapping precedence.
        """
        out = _sentinel_summary_from_events(
            [self._alarm(finding_type="custom", alarm_kind="ad_hoc")],
            now=NOW,
        )
        assert out["recent"][0]["finding_type"] == "custom"

    def test_alarm_severity_aggregates_with_findings(self):
        """One high alarm + one warning finding → by_severity {high:1, warning:1}.
        Severities mix freely — no separate alarm bucket.
        """
        events = [
            _event(event_id=1, severity="warning"),
            self._alarm(event_id=2, severity="high"),
        ]
        out = _sentinel_summary_from_events(events, now=NOW)
        assert out["by_severity"] == {"warning": 1, "high": 1}


class TestAuditRowFlatten:
    """`_sentinel_event_from_audit` adapts a durable audit.events row (finding
    fields nested under `details`) into the flat shape the aggregator consumes.

    This is what makes /v1/sentinel/summary survive restarts: the panel now
    reads persisted rows instead of the in-memory ring that's wiped on every
    governance-mcp restart. Pinning the mapping stops a future details-schema
    change from silently zeroing the panel."""

    def _row(self, **details):
        base_details = {
            "severity": "high",
            "violation_class": "coordinated_degradation",
            "finding_type": "cross_agent",
            "message": "coordinated EISV degradation across 3 agents",
        }
        base_details.update(details)
        return {
            "timestamp": NOW.isoformat(),
            "agent_id": "sentinel-uuid",
            "event_id": 42,
            "details": base_details,
        }

    def test_lifts_nested_detail_fields_to_top_level(self):
        flat = _sentinel_event_from_audit(self._row())
        assert flat["severity"] == "high"
        assert flat["violation_class"] == "coordinated_degradation"
        assert flat["finding_type"] == "cross_agent"
        assert flat["message"] == "coordinated EISV degradation across 3 agents"
        assert flat["timestamp"] == NOW.isoformat()
        assert flat["agent_id"] == "sentinel-uuid"
        assert flat["event_id"] == 42

    def test_alarm_kind_carried_through_for_fallback(self):
        """Alarm rows nest alarm_kind, not finding_type — the flattener must
        carry it so the aggregator's finding_type fallback works end-to-end."""
        flat = _sentinel_event_from_audit(
            self._row(finding_type=None, alarm_kind="ad_hoc")
        )
        assert flat["alarm_kind"] == "ad_hoc"

    def test_flattened_alarm_row_aggregates_into_summary(self):
        """End-to-end: a durable alarm row flattens and the aggregator falls
        finding_type back to alarm_kind, matching the in-memory ring path."""
        flat = _sentinel_event_from_audit(
            self._row(finding_type=None, alarm_kind="conflict_batch",
                      message="forced release: dialectic:/x")
        )
        out = _sentinel_summary_from_events([flat], now=NOW)
        assert out["total"] == 1
        assert out["recent"][0]["finding_type"] == "conflict_batch"

    def test_missing_details_does_not_crash(self):
        """A malformed row without `details` should flatten to a sparse event
        (counted, fields None) rather than raising and 500-ing the panel."""
        flat = _sentinel_event_from_audit({"timestamp": NOW.isoformat()})
        assert flat["severity"] is None
        assert flat["message"] is None
