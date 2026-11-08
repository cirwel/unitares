"""Local EISV frames: one agent, one instrument, one instrument id, advisory only."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.analysis import eisv_local_frame as lf

T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
SCRIPT = Path(lf.__file__)


def row(i, *, agent="a1", source="behavioral", iid="RR1:abc", carried=None, updates=100, E=0.7, I=0.8, S=0.2, V=0.0):
    sj = {"E": E}
    if source is not None:
        sj["sensor_eisv_source"] = source
    if iid is not None:
        sj["eisv_instrument"] = {"id": iid, "era": iid.split(":")[0], "carried_from": carried,
                                 "updates_since_change": updates}
    return {"identity_id": agent, "recorded_at": T0 + timedelta(minutes=i), "state_json": sj,
            "E": E, "I": I, "S": S, "V": V}


def test_insufficient_returns_no_numbers():
    f = lf.frame_for_agent([row(i) for i in range(10)], instrument="behavioral")
    assert f["status"] == "insufficient" and "axes" not in f


def test_deviation_against_frozen_reference():
    rows = [row(i, E=0.70 + (0.01 if i % 2 else -0.01)) for i in range(30)]
    rows += [row(30 + i, E=0.80) for i in range(5)]
    f = lf.frame_for_agent(rows, instrument="behavioral")
    assert f["status"] == "ok"
    e = f["axes"]["E"]
    assert e["raw_difference"] == pytest.approx(0.10, abs=1e-6)
    assert e["deviation"] == pytest.approx(0.10 / (1.4826 * 0.01), rel=1e-2)


def test_scale_floor_prevents_explosive_deviation():
    rows = [row(i, E=0.70) for i in range(30)] + [row(30 + i, E=0.705) for i in range(5)]
    e = lf.frame_for_agent(rows, instrument="behavioral")["axes"]["E"]
    assert e["scale_floored"] is True and e["deviation"] == pytest.approx(0.5, abs=0.01)


def test_other_instrument_and_untagged_rows_are_dropped():
    rows = [row(i) for i in range(35)] + [row(100 + i, source="physical") for i in range(5)]
    rows += [row(200 + i, source=None) for i in range(3)]
    f = lf.frame_for_agent(sorted(rows, key=lambda r: r["recorded_at"]), instrument="behavioral")
    assert f["dropped"] == {"other_instrument": 5, "no_source_tag": 3}
    assert f["n_settled"] == 35


def test_unstamped_rows_are_dropped():
    rows = [row(i, iid=None) for i in range(40)]
    f = lf.frame_for_agent(rows, instrument="behavioral")
    assert f["status"] == "insufficient" and f["dropped"] == {"unstamped": 40}


def test_only_the_latest_instrument_id_is_used():
    rows = [row(i, iid="I1:old", E=0.2) for i in range(40)] + [row(40 + i, iid="RR1:new", E=0.7) for i in range(35)]
    f = lf.frame_for_agent(rows, instrument="behavioral")
    assert f["instrument_id"] == "RR1:new" and f["earlier_ids_ignored"] == 40
    assert f["axes"]["E"]["reference_median"] == pytest.approx(0.7)


def test_restore_transient_is_excluded():
    rows = [row(i, carried="I1:old", updates=i) for i in range(60)]
    f = lf.frame_for_agent(rows, instrument="behavioral")
    assert f["n_settled"] == 10  # updates 50..59


def test_agents_are_framed_separately():
    rows = [row(i, agent="a") for i in range(35)] + [row(i, agent="b", E=0.3) for i in range(35)]
    out = lf.frames(rows, instrument="behavioral")
    assert set(out) == {"a", "b"}
    assert out["b"]["axes"]["E"]["reference_median"] == pytest.approx(0.3)


def test_unknown_instrument_is_refused():
    with pytest.raises(ValueError):
        lf.frames([], instrument="pooled")


def test_output_carries_no_decision_fields():
    rows = [row(i) for i in range(35)]
    f = lf.frame_for_agent(rows, instrument="behavioral")
    flat = repr(f).lower()
    for key in ("verdict", "action", "pause", "risk"):
        assert key not in flat
    assert f["advisory"] is True


def test_script_imports_nothing_from_the_decision_path():
    tree = ast.parse(SCRIPT.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("src.mcp_handlers", "src.governance", "src.monitor", "src.agent_storage", "src.db"))
                   for m in mods), mods


def test_sql_is_read_only():
    sql = lf.ROWS_SQL.upper()
    assert sql.strip().startswith("SELECT")
    for verb in ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"):
        assert verb not in sql


def test_read_window_starts_at_the_era_not_a_rolling_window(monkeypatch):
    era_start = datetime(2026, 10, 22, 14, 28, tzinfo=timezone.utc)
    monkeypatch.setattr(lf.eisv_eras, "current_era_window_start", lambda: (era_start, None))
    monkeypatch.setattr(lf.eisv_eras, "current_era_id", lambda: "RR1")
    assert lf.read_window() == (era_start, None, "RR1")
    assert "--days" not in SCRIPT.read_text()


def test_reference_stays_fixed_as_rows_accumulate():
    base = [row(i, E=0.70 + (0.01 if i % 2 else -0.01)) for i in range(30)]
    shift = [row(30 + i, E=0.80) for i in range(200)]
    f = lf.frame_for_agent(base + shift, instrument="behavioral")
    assert f["axes"]["E"]["reference_median"] == pytest.approx(0.70)
    assert f["axes"]["E"]["raw_difference"] == pytest.approx(0.10, abs=1e-6)


def test_stamp_only_era_is_passed_to_both_window_checks():
    text = SCRIPT.read_text()
    assert text.count("stamp_filtered=stamped") == 2
