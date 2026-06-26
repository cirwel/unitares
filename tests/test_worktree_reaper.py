"""Tests for the worktree reaper's removal decision.

The reaper exists because a naive "0 commits ahead" rule deleted a freshly-
created worktree another session had just opened. These tests pin the safety
invariants: merged authorizes, liveness/dirtiness vetoes.
"""
import contextlib
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "worktree_reaper",
    Path(__file__).resolve().parents[1] / "scripts" / "dev" / "worktree_reaper.py",
)
reaper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reaper)
decide = reaper.decide


def test_merged_and_idle_is_removed():
    action, _ = decide("MERGED", has_uncommitted_changes=False, idle_hours=48.0, min_idle_hours=12.0)
    assert action == "remove"


def test_unmerged_never_removed():
    for state in ("OPEN", "CLOSED", None):
        action, reason = decide(state, False, 48.0, 12.0)
        assert action == "skip"
        assert "not merged" in reason


def test_recently_active_merged_is_kept():
    # The 2026-06-18 failure mode: a merged-ish worktree touched minutes ago.
    action, reason = decide("MERGED", False, idle_hours=0.2, min_idle_hours=12.0)
    assert action == "skip"
    assert "active" in reason


def test_dirty_merged_is_kept():
    action, reason = decide("MERGED", has_uncommitted_changes=True, idle_hours=999.0, min_idle_hours=12.0)
    assert action == "skip"
    assert "uncommitted" in reason


def test_unknown_liveness_is_kept():
    action, reason = decide("MERGED", False, idle_hours=None, min_idle_hours=12.0)
    assert action == "skip"
    assert "liveness unknown" in reason


def test_dirtiness_beats_merged_even_if_idle():
    # Order invariant: dirtiness vetoes before merged status authorizes.
    action, _ = decide("MERGED", has_uncommitted_changes=True, idle_hours=0.1, min_idle_hours=12.0)
    assert action == "skip"


def test_idle_floor_is_inclusive_boundary():
    # Exactly at the floor counts as idle-enough (>= floor removes).
    assert decide("MERGED", False, 12.0, 12.0)[0] == "remove"
    assert decide("MERGED", False, 11.99, 12.0)[0] == "skip"


def test_main_invokes_lease_advisory_with_expected_surface(monkeypatch, capsys):
    from unitares_sdk.lease_plane import advisory as advisory_module

    captured = {}
    events = []

    @contextlib.contextmanager
    def fake_scope(**kwargs):
        captured.update(kwargs)
        events.append("enter")
        yield ("held_by_other", None)
        events.append("exit")

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", fake_scope)
    monkeypatch.setattr(reaper, "_pr_states", lambda: {})
    monkeypatch.setattr(reaper, "_worktrees", lambda: [])

    rc = reaper.main(["--json", "--min-idle-hours", "24"])

    assert rc == 0
    assert events == ["enter", "exit"]
    assert captured["surface_id"] == "resident:/worktree_reaper"
    assert captured["ttl_s"] == 900
    assert "worktree reaper dry-run" in captured["intent"]
    assert "min_idle_hours=24" in captured["intent"]
    assert '"plan": []' in capsys.readouterr().out
