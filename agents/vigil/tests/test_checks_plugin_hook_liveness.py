"""Tests for the PluginHookLiveness Vigil check.

The check is a divergence test (recent CC activity + stale hook artifact =
dark). The pure assess() carries the logic; tests drive it with synthetic
mtimes and a fixed ``now`` so there is no real clock or filesystem coupling.
"""

from __future__ import annotations

import asyncio

import pytest

HOUR = 3600.0
NOW = 1_000_000.0  # fixed synthetic clock


def _reset_registry():
    from agents.vigil.checks import registry
    registry._CHECKS.clear()
    registry._LOADED = False


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_registry()
    yield
    _reset_registry()


def _write(path, age_hours):
    """Create a file and backdate its mtime by age_hours relative to NOW."""
    path.write_text("x")
    import os
    os.utime(path, (NOW - age_hours * HOUR, NOW - age_hours * HOUR))
    return str(path)


def test_identity():
    from agents.vigil.checks.plugin_hook_liveness import PluginHookLiveness

    check = PluginHookLiveness()
    assert check.name == "plugin_hook_liveness"
    assert check.service_key == "governance"


def test_registered_as_builtin():
    from agents.vigil.checks import registry

    registry.load_plugins()
    names = {getattr(c, "name", "") for c in registry.all_checks()}
    assert "plugin_hook_liveness" in names


def test_live_when_activity_and_hook_both_recent(tmp_path):
    from agents.vigil.checks import plugin_hook_liveness as p

    history = _write(tmp_path / "history.jsonl", age_hours=1)
    artifact = _write(tmp_path / "checkins.log", age_hours=2)

    res = p.assess(history, [artifact], NOW, activity_hours=12, stale_hours=24)
    assert res.ok is True
    assert "live" in res.summary


def test_dark_when_activity_recent_but_hook_stale(tmp_path):
    """The 2026-06-02 case: CC active daily, checkins.log frozen for weeks."""
    from agents.vigil.checks import plugin_hook_liveness as p

    history = _write(tmp_path / "history.jsonl", age_hours=2)
    artifact = _write(tmp_path / "checkins.log", age_hours=17 * 24)  # 17 days

    res = p.assess(history, [artifact], NOW, activity_hours=12, stale_hours=24)
    assert res.ok is False
    assert res.severity == "warning"
    assert res.fingerprint_key == "plugin_hook_chain_dark"
    assert res.detail["hook_age_hours"] == pytest.approx(17 * 24, rel=1e-3)


def test_dark_when_no_hook_artifact_exists(tmp_path):
    from agents.vigil.checks import plugin_hook_liveness as p

    history = _write(tmp_path / "history.jsonl", age_hours=1)
    missing = str(tmp_path / "nope.log")

    res = p.assess(history, [missing], NOW, activity_hours=12, stale_hours=24)
    assert res.ok is False
    assert res.fingerprint_key == "plugin_hook_chain_dark"
    assert res.detail["hook_artifact_mtime"] is None


def test_newest_artifact_wins_so_skip_log_keeps_chain_live(tmp_path):
    """A gated-but-dispatching chain (skip log fresh, checkin log old) is alive."""
    from agents.vigil.checks import plugin_hook_liveness as p

    history = _write(tmp_path / "history.jsonl", age_hours=1)
    old_checkins = _write(tmp_path / "checkins.log", age_hours=30 * 24)
    fresh_skips = _write(tmp_path / "hook-skips.log", age_hours=1)

    res = p.assess(
        history, [old_checkins, fresh_skips], NOW, activity_hours=12, stale_hours=24
    )
    assert res.ok is True


def test_indeterminate_when_operator_idle(tmp_path):
    """Old activity must NOT page — idle is not dark."""
    from agents.vigil.checks import plugin_hook_liveness as p

    history = _write(tmp_path / "history.jsonl", age_hours=48)
    artifact = _write(tmp_path / "checkins.log", age_hours=48)

    res = p.assess(history, [artifact], NOW, activity_hours=12, stale_hours=24)
    assert res.ok is True
    assert "indeterminate" in res.summary


def test_indeterminate_when_no_history(tmp_path):
    from agents.vigil.checks import plugin_hook_liveness as p

    res = p.assess(
        str(tmp_path / "absent.jsonl"), [], NOW, activity_hours=12, stale_hours=24
    )
    assert res.ok is True
    assert "indeterminate" in res.summary


def test_run_wires_real_paths(tmp_path, monkeypatch):
    """run() reads module-level config, so monkeypatching paths takes effect."""
    from agents.vigil.checks import plugin_hook_liveness as p

    history = tmp_path / "history.jsonl"
    history.write_text("x")  # fresh (now-ish)
    artifact = tmp_path / "checkins.log"
    artifact.write_text("x")  # fresh

    monkeypatch.setattr(p, "HISTORY_PATH", str(history))
    monkeypatch.setattr(p, "HOOK_ARTIFACT_PATHS", [str(artifact)])

    res = asyncio.run(p.PluginHookLiveness().run())
    assert res.ok is True
