"""Shared isolation for watcher tests (#652).

Found live during the 2026-06-12 strict-identity burn-in: the watcher
test suite was making REAL HTTP calls to the production governance
server (unbound knowledge writes from sandboxed test state — refused
under STRICT_IDENTITY_REQUIRED, silently auto-minting ghost identities
before it) and appending test output to the real
~/Library/Logs/unitares-watcher.log.

Three seams close these leaks for every test in this directory:

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

3. ``UNITARES_WATCHER_DATA_DIR`` and the path-resolution cache point at the
   test's tmp dir. Cross-process model-slot tests inherit the override, so
   they contend with their own child processes without touching the live
   ``~/.unitares/watcher/model_slot.lock``.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

from agents.watcher import _util as watcher_util

_REAL_LOG = Path.home() / "Library" / "Logs" / "unitares-watcher.log"


@pytest.fixture(autouse=True)
def _watcher_isolation(monkeypatch, tmp_path, request):
    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "watcher tests must not perform real network I/O (#652) — "
            "mock the client/HTTP layer instead of letting the call "
            "reach a live service"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)

    sandbox_state = tmp_path / "watcher-state"
    monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(sandbox_state))
    monkeypatch.setattr(watcher_util, "_state_dir_cache", None)

    # The identity anchor is NOT under UNITARES_WATCHER_DATA_DIR: agent.py
    # computes SESSION_FILE from Path.home() at import time, so the env var
    # above does not move it. Two consequences, both real:
    #   - reads leak. Any code path that resolves Watcher's UUID picks up the
    #     developer's actual identity, so the suite passes on CI (no anchor)
    #     and fails locally, or vice versa.
    #   - writes are worse. _save_session() would overwrite the real anchor,
    #     which carries a LIVE continuity token at 0600.
    # Sandbox both so tests can never touch it.
    # Opt-out for the two tests whose whole subject IS the real anchor path
    # (TestSessionAnchor). Marker rather than a name check so the exemption is
    # declared at the test, not guessed here.
    if "real_session_anchor" not in request.keywords:
        sandbox_anchor = tmp_path / "anchors" / "watcher.json"
        sandbox_anchor.parent.mkdir(parents=True, exist_ok=True)
        for mod in list(sys.modules.values()):
            try:
                if getattr(mod, "SESSION_FILE", None) is not None:
                    monkeypatch.setattr(mod, "SESSION_FILE", sandbox_anchor, raising=False)
                if getattr(mod, "LEGACY_SESSION_FILE", None) is not None:
                    monkeypatch.setattr(mod, "LEGACY_SESSION_FILE",
                                        tmp_path / ".watcher_session", raising=False)
            except Exception:
                continue

    sandbox_log = tmp_path / "unitares-watcher.log"
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "LOG_FILE", None) == _REAL_LOG:
                monkeypatch.setattr(mod, "LOG_FILE", sandbox_log)
        except Exception:
            continue
    yield sandbox_state
