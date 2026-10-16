"""Era guard in scripts/dev/grounding_flip_analysis.py.

The read matches each grounding event to state rows up to JOIN_SLACK away, so
the instrument check must cover the event window widened by that much, and it
runs after the read because each psql call is its own snapshot.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "grounding_flip_analysis.py"
_NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)  # inside RK1, hours after its restart


class _FixedNow:
    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now


@pytest.fixture
def gfa(monkeypatch):
    spec = importlib.util.spec_from_file_location("grounding_flip_analysis_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "datetime", _FixedNow(_NOW))
    return module


def test_the_id_check_covers_the_join_slack_and_runs_after_the_read(gfa, monkeypatch):
    calls = []
    monkeypatch.setattr(gfa, "fetch", lambda db, s, e: calls.append(("fetch", s, e)) or [])

    def ids(db, s, e):
        calls.append(("ids", s, e))
        return {"RK1:aaa": 4}

    monkeypatch.setattr(gfa, "fetch_instrument_ids", ids)
    assert gfa.main(["--hours", "1"]) == 0
    start = _NOW - timedelta(hours=1)
    assert calls == [
        ("fetch", start, _NOW),
        ("ids", start - gfa.JOIN_SLACK, _NOW + gfa.JOIN_SLACK + timedelta(microseconds=1)),
    ]


def test_a_second_instrument_id_in_the_slack_is_refused(gfa, monkeypatch, capsys):
    monkeypatch.setattr(gfa, "fetch", lambda *a: [])
    monkeypatch.setattr(gfa, "fetch_instrument_ids", lambda *a: {"RK1:aaa": 4, "RK1:bbb": 1})
    assert gfa.main(["--hours", "1"]) == 2
    assert "2 different EISV instrument ids" in capsys.readouterr().err


def test_a_crossing_window_is_refused_before_any_query(gfa, monkeypatch, capsys):
    def no_db(*_a):
        raise AssertionError("psql must not run")

    monkeypatch.setattr(gfa, "_query", no_db)
    assert gfa.main([]) == 2
    assert capsys.readouterr().err.startswith("refused:")


def test_analysis_never_pools_sensor_classes(gfa, capsys):
    row = dict(e=0.8, i=0.8, s=0.1, v=0.0, risk=0.1, coh_ung=0.6, coh_grd=0.6,
               label="agent-x", age_h=1.0)
    rows = [dict(row, sensor_class="physical")] * 3 + [dict(row, sensor_class="behavioral")] * 2
    gfa.analyze(rows, window_h=24.0, min_near=300, min_flips=10)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "agent-x" in line and line[:1] != " "]
    assert len(lines) == 2
    assert any(line.startswith("behavioral") and "   2 " in line for line in lines)
    assert any(line.startswith("physical") and "   3 " in line for line in lines)
