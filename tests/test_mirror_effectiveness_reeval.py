"""Tests for scripts/dev/mirror_effectiveness_reeval.py (Phase 1 analysis)."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def mod():
    path = project_root / "scripts" / "dev" / "mirror_effectiveness_reeval.py"
    spec = importlib.util.spec_from_file_location("mirror_effectiveness_reeval", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mirror_effectiveness_reeval"] = module
    spec.loader.exec_module(module)
    return module


def _emit(agent_id, update_index, surfaced, signal_type, value):
    return {
        "agent_id": agent_id,
        "ts": "2026-06-15T00:00:00+00:00",
        "update_index": update_index,
        "surfaced": surfaced,
        "signals": [{"signal_type": signal_type, "value": value, "threshold": 0.005}],
    }


# ---------------------------------------------------------------------------
# flatten_emissions
# ---------------------------------------------------------------------------

def test_flatten_explodes_signals(mod):
    events = [{
        "agent_id": "a1", "ts": "t", "update_index": 4, "surfaced": True,
        "signals": [
            {"signal_type": "autopilot_complexity", "value": 0.001},
            {"signal_type": "autopilot_confidence", "value": 0.0},
        ],
    }]
    rows = mod.flatten_emissions(events)
    assert len(rows) == 2
    assert {r["signal_type"] for r in rows} == {"autopilot_complexity", "autopilot_confidence"}
    assert all(r["agent_id"] == "a1" and r["surfaced"] is True for r in rows)


def test_flatten_drops_rows_without_agent_or_value(mod):
    events = [
        {"agent_id": None, "signals": [{"signal_type": "x", "value": 0.1}]},
        {"agent_id": "a1", "signals": [{"signal_type": "x", "value": None}]},
        {"agent_id": "a1", "signals": [{"signal_type": "x", "value": "nan-ish"}]},
    ]
    assert mod.flatten_emissions(events) == []


# ---------------------------------------------------------------------------
# evaluate_signal — verdict logic
# ---------------------------------------------------------------------------

def test_insufficient_data_when_cohort_too_small(mod):
    # 2 surfaced agents, 0 shadow -> insufficient
    rows = mod.flatten_emissions([
        _emit("a1", 1, True, "autopilot_complexity", 0.001),
        _emit("a1", 2, True, "autopilot_complexity", 0.004),
        _emit("a2", 1, True, "autopilot_complexity", 0.000),
        _emit("a2", 2, True, "autopilot_complexity", 0.003),
    ])
    v = mod.evaluate_signal(rows, "autopilot_complexity", min_agents=5)
    assert v.verdict == "insufficient_data"


def _cohort(mod, signal_type, surfaced, agents, first_val, last_val):
    """Build emissions: each agent fires twice (val first then last)."""
    rows = []
    for i in range(agents):
        aid = f"{'s' if surfaced else 'h'}{i}"
        rows.append(_emit(aid, 1, surfaced, signal_type, first_val))
        rows.append(_emit(aid, 2, surfaced, signal_type, last_val))
    return rows


def test_effective_when_surfaced_beats_shadow_in_direction(mod):
    # autopilot: higher variance trend is better (direction +1).
    # surfaced agents rise 0.001 -> 0.004 (trend +0.003);
    # shadow agents stay flat 0.001 -> 0.001 (trend 0.0).
    st = "autopilot_complexity"
    rows = mod.flatten_emissions(
        _cohort(mod, st, True, 5, 0.001, 0.004) + _cohort(mod, st, False, 5, 0.001, 0.001)
    )
    v = mod.evaluate_signal(rows, st, min_agents=5)
    assert v.verdict == "effective"
    assert v.improvement == pytest.approx(0.003, abs=1e-9)
    assert v.surfaced.n_agents == 5 and v.shadow.n_agents == 5


def test_no_measurable_effect_when_surfaced_not_better(mod):
    st = "autopilot_complexity"
    # surfaced flat, shadow rises -> surfaced advantage negative -> no effect
    rows = mod.flatten_emissions(
        _cohort(mod, st, True, 5, 0.001, 0.001) + _cohort(mod, st, False, 5, 0.001, 0.004)
    )
    v = mod.evaluate_signal(rows, st, min_agents=5)
    assert v.verdict == "no_measurable_effect"
    assert v.improvement < 0


def test_divergence_direction_is_lower_better(mod):
    # complexity_divergence: direction -1. surfaced falls 0.5 -> 0.2 (good),
    # shadow stays 0.5 -> 0.5. improvement = -1 * (surfaced_mean - shadow_mean)
    #   = -1 * (-0.3 - 0.0) = +0.3 > 0 -> effective.
    st = "complexity_divergence"
    rows = mod.flatten_emissions(
        _cohort(mod, st, True, 5, 0.5, 0.2) + _cohort(mod, st, False, 5, 0.5, 0.5)
    )
    v = mod.evaluate_signal(rows, st, min_agents=5)
    assert v.direction == -1
    assert v.verdict == "effective"
    assert v.improvement == pytest.approx(0.3, abs=1e-9)


def test_single_firing_agents_excluded_from_trend(mod):
    # Agents with one firing have no trend and don't count toward cohort n.
    rows = mod.flatten_emissions([
        _emit("a1", 1, True, "autopilot_complexity", 0.001),  # only one firing
    ])
    v = mod.evaluate_signal(rows, "autopilot_complexity", min_agents=1)
    assert v.surfaced.n_agents == 0
    assert v.verdict == "insufficient_data"


def test_cohort_assigned_by_first_firing_surfaced(mod):
    # First firing shadow, later firing surfaced -> agent is shadow cohort.
    rows = mod.flatten_emissions([
        _emit("a1", 1, False, "autopilot_complexity", 0.001),
        _emit("a1", 5, True, "autopilot_complexity", 0.004),
    ])
    trends = mod._agent_trends(rows, "autopilot_complexity")
    assert trends["a1"]["first_surfaced"] is False
    assert trends["a1"]["trend"] == pytest.approx(0.003, abs=1e-9)


def test_ordering_uses_update_index_not_input_order(mod):
    # Out-of-order input; trend must use update_index ordering (1 -> 9).
    rows = mod.flatten_emissions([
        _emit("a1", 9, True, "autopilot_complexity", 0.004),
        _emit("a1", 1, True, "autopilot_complexity", 0.001),
    ])
    trends = mod._agent_trends(rows, "autopilot_complexity")
    assert trends["a1"]["trend"] == pytest.approx(0.003, abs=1e-9)


# ---------------------------------------------------------------------------
# JSONL loader + window filter
# ---------------------------------------------------------------------------

def test_jsonl_loader_filters_event_type_and_window(mod, tmp_path):
    log = tmp_path / "audit.jsonl"
    in_window = {
        "timestamp": "2026-06-15T12:00:00+00:00", "agent_id": "a1",
        "event_type": "mirror_signal.emit",
        "details": {"update_index": 3, "surfaced": True,
                    "signals": [{"signal_type": "autopilot_complexity", "value": 0.001}]},
    }
    out_of_window = dict(in_window)
    out_of_window["timestamp"] = "2026-01-01T00:00:00+00:00"
    other_event = {"timestamp": "2026-06-15T12:00:00+00:00", "agent_id": "a1",
                   "event_type": "lambda1_skip", "details": {}}
    with open(log, "w") as f:
        for e in (in_window, out_of_window, other_event):
            f.write(json.dumps(e) + "\n")

    start = datetime(2026, 6, 15, tzinfo=timezone.utc)
    end = datetime(2026, 6, 16, tzinfo=timezone.utc)
    events = mod._load_from_jsonl(str(log), start, end)
    assert len(events) == 1
    assert events[0]["agent_id"] == "a1"
    assert events[0]["surfaced"] is True


def test_evaluate_all_covers_every_known_signal(mod):
    verdicts = mod.evaluate_all([], min_agents=5, min_effect=0.0)
    assert {v.signal_type for v in verdicts} == set(mod.SIGNAL_DIRECTION)
    assert all(v.verdict == "insufficient_data" for v in verdicts)
