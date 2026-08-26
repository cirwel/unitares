"""Verdict-logic coverage for the effort-profile degeneracy check.

The script implements §14 of docs/proposals/eisv-effort-profile-channel-v0.md,
whose thresholds were committed before any corpus was measured. These tests pin
the behaviour that section specifies -- in particular that the support gate
fires before any degeneracy claim, and that the cycle markers can never produce
a KILL on their own.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "effort_profile_degeneracy.py"


def _load():
    spec = importlib.util.spec_from_file_location("effort_profile_degeneracy", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sessions(count, identity, turns, duration, cycles):
    return [
        {
            "session": f"{identity}-{i}",
            "identity_proxy": identity,
            "turns": turns(i),
            "duration_s": duration(i),
            "cycles": cycles(i),
        }
        for i in range(count)
    ]


def _across(identities, count, turns, duration, cycles):
    out = []
    for identity in identities:
        out.extend(_sessions(count, identity, turns, duration, cycles))
    return out


VARIED_TURNS = lambda i: 3 + i * 2          # noqa: E731
VARIED_DURATION = lambda i: 300 + i * 400   # noqa: E731
SOME_CYCLES = lambda i: i % 3               # noqa: E731
NO_CYCLES = lambda i: 0                     # noqa: E731


def test_support_gate_fires_before_any_degeneracy_claim():
    """Too few identities is UNDERPOWERED, never a degeneracy finding."""
    module = _load()
    result = module.evaluate(
        _across(("a", "b"), 20, VARIED_TURNS, VARIED_DURATION, SOME_CYCLES)
    )
    assert result["verdicts"] == ["UNDERPOWERED"]
    assert result["kill"] is False


def test_variance_present_passes():
    module = _load()
    result = module.evaluate(
        _across(("a", "b", "c"), 12, VARIED_TURNS, VARIED_DURATION, SOME_CYCLES)
    )
    assert result["verdicts"] == ["PASS"]
    assert result["kill"] is False


def test_uniform_duration_kills():
    """Coefficient of variation below the floor closes the channel."""
    module = _load()
    result = module.evaluate(
        _across(("a", "b", "c"), 12, VARIED_TURNS, lambda i: 1000.0, SOME_CYCLES)
    )
    assert "KILL" in result["verdicts"]
    assert result["kill"] is True


def test_low_median_turns_kills():
    module = _load()
    result = module.evaluate(
        _across(("a", "b", "c"), 12, lambda i: 1, VARIED_DURATION, NO_CYCLES)
    )
    assert "KILL" in result["verdicts"]


def test_absent_cycles_never_kill_on_their_own():
    """Zero cycles beside varying turns and duration indicts the markers only."""
    module = _load()
    result = module.evaluate(
        _across(("a", "b", "c"), 12, VARIED_TURNS, VARIED_DURATION, NO_CYCLES)
    )
    assert result["kill"] is False
    assert "PASS" in result["verdicts"]
    assert "MARKER-SET-BLIND" in result["verdicts"]


def test_within_agent_variance_is_not_between_agent_spread():
    module = _load()
    sessions = _sessions(30, "a", VARIED_TURNS, VARIED_DURATION, SOME_CYCLES)
    sessions += _sessions(2, "b", lambda i: 5, lambda i: 900, lambda i: 1)
    sessions += _sessions(2, "c", lambda i: 7, lambda i: 1200, NO_CYCLES)
    result = module.evaluate(sessions)
    assert "SINGLE-AGENT" in result["verdicts"]


def test_thresholds_match_the_committed_section():
    """Guards against a verdict being moved by editing the script."""
    module = _load()
    assert module.MIN_SESSIONS == 30
    assert module.MIN_IDENTITIES == 3
    assert module.MIN_SESSIONS_PER_IDENTITY == 10
    assert module.KILL_MEDIAN_TURNS == 2
    assert module.KILL_DURATION_CV == 0.25
    assert module.MARKER_BLIND_ZERO_FRACTION == 0.90


def test_tool_results_and_injected_reminders_are_not_human_turns(tmp_path):
    """A `type: user` record is usually a tool result, not authored text."""
    module = _load()
    rows = [
        {"type": "user", "timestamp": "2026-08-26T10:00:00Z",
         "message": {"role": "user", "content": "please do the thing"}},
        {"type": "user", "timestamp": "2026-08-26T10:01:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result"}]}},
        {"type": "user", "timestamp": "2026-08-26T10:02:00Z",
         "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
        {"type": "assistant", "timestamp": "2026-08-26T10:03:00Z",
         "message": {"role": "assistant", "content": "ok"}},
        {"type": "user", "timestamp": "2026-08-26T10:30:00Z",
         "message": {"role": "user", "content": "actually, do it the other way"}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))

    profile = module.read_session(path)
    assert profile["turns"] == 2          # tool_result and reminder excluded
    assert profile["cycles"] == 1         # only the "actually" turn matches
    assert profile["duration_s"] == 1800.0
