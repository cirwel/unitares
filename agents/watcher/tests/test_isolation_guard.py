"""Pins the #652 isolation guard: no real network, no real log, no real
calibration floor."""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest


def test_urlopen_is_blocked():
    with pytest.raises(RuntimeError, match="must not perform real network"):
        urllib.request.urlopen("http://127.0.0.1:8767/health")


def test_log_file_is_sandboxed():
    import agents.watcher._util as _util
    real = Path.home() / "Library" / "Logs" / "unitares-watcher.log"
    assert _util.LOG_FILE != real
    # And the shared log() helper writes to the sandbox, not the real file.
    _util.log("isolation guard probe", "info")
    assert _util.LOG_FILE.exists()


def test_calibration_floor_is_sandboxed(_watcher_isolation):
    """The floor's default dir is bound when floor_state is imported, so the
    env override alone leaves it on the real ``~/.unitares/watcher``. Every
    findings render reads it, and a developer's floor demoted and hid the
    listing tests' fixtures. Checking the path fails on CI too, where no
    floor demotes those fixtures and the leak itself changes no result."""
    from agents.watcher import findings, floor_state
    from agents.watcher.calibration import BucketStats

    sandbox = _watcher_isolation
    assert floor_state.DEFAULT_STATE_DIR == sandbox

    # The renderer's own default read lands in the sandbox. The file is
    # produced by the real serializer but written by hand: save_floor() with
    # no state_dir would write the real floor if this guard ever regressed.
    state = floor_state.FloorState(
        updated_at="2026-01-01T00:00:00Z",
        buckets={
            ("P006", "app"): BucketStats(
                pattern="P006",
                file_class="app",
                weighted_confirmed=0.0,
                weighted_dismissed=24.4,
                weighted_n=24.4,
                ci_lower=0.0,
                latest_observation="2026-01-01T00:00:00Z",
            )
        },
    )
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / floor_state.FLOOR_FILE_NAME).write_text(floor_state._floor_json(state))

    loaded = findings.load_floor()
    assert loaded.updated_at == "2026-01-01T00:00:00Z"
    assert loaded.get("P006", "app").ci_lower == 0.0
