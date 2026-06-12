"""Pins the #652 isolation guard: no real network, no real log."""
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
