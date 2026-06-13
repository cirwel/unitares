"""Shared isolation for watcher tests (#652).

Found live during the 2026-06-12 strict-identity burn-in: the watcher
test suite was making REAL HTTP calls to the production governance
server (unbound knowledge writes from sandboxed test state — refused
under STRICT_IDENTITY_REQUIRED, silently auto-minting ghost identities
before it) and appending test output to the real
~/Library/Logs/unitares-watcher.log.

Two seams close both leaks for every test in this directory:

1. ``urllib.request.urlopen`` is replaced with a hard failure. Both the
   SDK sync client (governance REST) and the agent's Ollama call go
   through it at call time, so no code path can reach a live service.
   Tests that exercise HTTP behavior mock at a higher layer and never
   hit this.

2. Every loaded module whose ``LOG_FILE`` points at the real watcher
   log is repointed into the test's tmp dir. The sys.modules sweep is
   needed because several tests load agent.py via
   ``importlib.util.spec_from_file_location`` under ad-hoc names, each
   binding its own copy of the constant.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

_REAL_LOG = Path.home() / "Library" / "Logs" / "unitares-watcher.log"


@pytest.fixture(autouse=True)
def _watcher_isolation(monkeypatch, tmp_path):
    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "watcher tests must not perform real network I/O (#652) — "
            "mock the client/HTTP layer instead of letting the call "
            "reach a live service"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)

    sandbox_log = tmp_path / "unitares-watcher.log"
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "LOG_FILE", None) == _REAL_LOG:
                monkeypatch.setattr(mod, "LOG_FILE", sandbox_log)
        except Exception:
            continue
    yield
