"""Tests for the pure helpers behind /v1/vigil/summary.

The endpoint itself stitches together ``_vigil_agent_id`` (label resolver),
``_vigil_cycle_history`` (broadcaster filter), ``_recent_writes_for_agent``
(shared with residents), and ``_vigil_stats`` (rollup). The aggregator is
pure so we test it directly; the agent-id resolver is tested against a
hand-rolled mcp_server stub.

Vigil is a janitorial resident — every cycle emits a low-severity
groundskeeper note, and the panel needs to segregate that noise out of the
main Discoveries feed rather than re-summarise it. So the panel data shape
is: cycles (from check-ins) + writes (from KG) + rollup stats.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agents.vigil.routes import _vigil_agent_id, _vigil_stats


# Anchor on real wall-clock so offsets stay consistent with `time.time()`
# inside _vigil_stats. A fixed datetime here goes stale once real time
# advances past the 24h window, breaking cycles_24h / writes_24h tests
# in CI on any day after the constant's value.
NOW = datetime.now(timezone.utc)
NOW_TS = NOW.timestamp()


def _cycle(offset_minutes=0, coherence=0.5, verdict="proceed"):
    ts = NOW - timedelta(minutes=offset_minutes)
    return {
        "timestamp": ts.isoformat(),
        "ts": ts.timestamp(),
        "E": 0.6, "I": 0.7, "S": 0.3, "V": 0.1,
        "coherence": coherence,
        "risk": 0.1,
        "verdict": verdict,
    }


def _write(offset_minutes=0, severity="low", summary="Groundskeeper: 5 stale"):
    ts = NOW - timedelta(minutes=offset_minutes)
    return {
        "id": f"d-{offset_minutes}",
        "type": "note",
        "severity": severity,
        "summary": summary,
        "tags": [],
        "timestamp": ts.isoformat(),
    }


class TestVigilStats:
    def test_empty_inputs_return_null_stats(self):
        out = _vigil_stats([], [])
        assert out["last_cycle_at"] is None
        assert out["last_cycle_age_seconds"] is None
        assert out["cycles_24h"] == 0
        assert out["writes_24h"] == 0
        assert out["avg_coherence_window"] is None
        assert out["last_verdict"] is None
        assert out["total_cycles_in_window"] == 0

    def test_last_cycle_is_first_entry(self):
        """Cycles are passed newest-first; stats mirror that without re-sorting."""
        cycles = [_cycle(0, verdict="proceed"), _cycle(30, verdict="guide")]
        out = _vigil_stats(cycles, [])
        assert out["last_cycle_at"] == cycles[0]["timestamp"]
        assert out["last_verdict"] == "proceed"

    def test_cycles_24h_counts_only_window(self):
        cycles = [
            _cycle(0),
            _cycle(60),
            _cycle(12 * 60),
            _cycle(25 * 60),  # just outside
            _cycle(48 * 60),
        ]
        out = _vigil_stats(cycles, [])
        assert out["cycles_24h"] == 3

    def test_writes_24h_parses_iso_timestamps(self):
        writes = [
            _write(5),
            _write(60),
            _write(25 * 60),  # outside 24h
        ]
        out = _vigil_stats([], writes)
        assert out["writes_24h"] == 2

    def test_writes_with_bad_timestamps_ignored(self):
        writes = [
            _write(5),
            {"id": "bad", "timestamp": "not-a-date", "severity": "low"},
            {"id": "missing", "severity": "low"},  # no timestamp at all
        ]
        out = _vigil_stats([], writes)
        # The bad/missing ones are silently skipped — showing them as zero'd
        # counts would be less honest than counting just the parseable ones.
        assert out["writes_24h"] == 1

    def test_avg_coherence_ignores_missing_values(self):
        cycles = [
            _cycle(0, coherence=0.4),
            _cycle(30, coherence=0.6),
            _cycle(60, coherence=None),
        ]
        out = _vigil_stats(cycles, [])
        assert out["avg_coherence_window"] == 0.5

    def test_totals_reflect_full_input_not_window(self):
        """total_cycles_in_window is whatever the caller passed in — the panel
        uses this for the footer count, which should match what it displays
        in the stream, not a re-filtered subset."""
        cycles = [_cycle(0), _cycle(48 * 60)]  # one outside 24h
        out = _vigil_stats(cycles, [])
        assert out["total_cycles_in_window"] == 2
        assert out["cycles_24h"] == 1


class TestVigilAgentId:
    """Resolver picks the active Vigil row from mcp_server.agent_metadata.

    The concrete risk this guards: when governance-mcp restarts create
    duplicate Vigil rows, the dashboard must track the one actually running,
    not a stale archived copy. Mirrors the preference logic in http_residents.
    """

    def _srv(self, metadata):
        return SimpleNamespace(agent_metadata=metadata)

    def test_returns_none_when_no_vigil_registered(self):
        srv = self._srv({"uuid-a": SimpleNamespace(label="Sentinel", status="active")})
        assert _vigil_agent_id(srv) is None

    def test_picks_vigil_by_label_case_insensitive(self):
        srv = self._srv({
            "uuid-a": SimpleNamespace(label="vigil", status="active", total_updates=10),
        })
        assert _vigil_agent_id(srv) == "uuid-a"

    def test_prefers_active_over_archived(self):
        srv = self._srv({
            "uuid-archived": SimpleNamespace(label="Vigil", status="archived", total_updates=1000),
            "uuid-active": SimpleNamespace(label="Vigil", status="active", total_updates=5),
        })
        assert _vigil_agent_id(srv) == "uuid-active"

    def test_within_same_tier_prefers_more_updates(self):
        srv = self._srv({
            "uuid-quiet": SimpleNamespace(label="Vigil", status="active", total_updates=3),
            "uuid-busy": SimpleNamespace(label="Vigil", status="active", total_updates=500),
        })
        assert _vigil_agent_id(srv) == "uuid-busy"

    def test_falls_back_to_display_name_when_label_missing(self):
        srv = self._srv({
            "uuid-a": SimpleNamespace(label=None, display_name="Vigil",
                                      status="active", total_updates=1),
        })
        assert _vigil_agent_id(srv) == "uuid-a"
