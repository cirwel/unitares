"""Watcher silent-fork guard: resolve_identity refuses to fresh-onboard
when the anchor is missing and UNITARES_FIRST_RUN is not set.

Added 2026-04-19 as Phase 3 of the anchor-resilience series. Watcher does
NOT inherit GovernanceAgent, so the SDK's refuse_fresh_onboard flag does
not cover it — the guard is open-coded in resolve_identity.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def watcher_module():
    """Load agents/watcher/agent.py as a module."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    module_path = project_root / "agents" / "watcher" / "agent.py"
    spec = importlib.util.spec_from_file_location("watcher_agent", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["watcher_agent"] = module
    spec.loader.exec_module(module)
    return module


class _MockClient:
    client_session_id = "sess"
    continuity_token = "tok"
    agent_uuid = "uuid"

    def __init__(self):
        self.onboard_calls = 0
        self.identity_calls = 0

    def onboard(self, *a, **kw):
        self.onboard_calls += 1
        return type("R", (), {"success": True})()

    def identity(self, **kw):
        self.identity_calls += 1
        return type("R", (), {"success": True})()


def test_watcher_refuses_fresh_onboard_without_first_run(
    watcher_module, monkeypatch, tmp_path
):
    """With no anchor and no UNITARES_FIRST_RUN, resolve_identity must
    NOT call client.onboard. _watcher_identity stays None."""
    session_file = tmp_path / "watcher.json"
    monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    monkeypatch.setattr(watcher_module, "_watcher_identity", None)

    client = _MockClient()
    watcher_module.resolve_identity(client)

    assert client.onboard_calls == 0
    assert watcher_module.get_watcher_identity() is None


def test_watcher_allows_fresh_onboard_when_first_run_set(
    watcher_module, monkeypatch, tmp_path
):
    """With UNITARES_FIRST_RUN=1, fresh onboard is permitted — operator
    explicitly authorized a new identity."""
    session_file = tmp_path / "watcher.json"
    monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
    monkeypatch.setenv("UNITARES_FIRST_RUN", "1")
    monkeypatch.setattr(watcher_module, "_watcher_identity", None)

    # Avoid stamping attempt after onboard (call_tool not on _MockClient)
    client = _MockClient()
    client.call_tool = lambda *a, **kw: None
    watcher_module.resolve_identity(client)

    assert client.onboard_calls == 1
    identity = watcher_module.get_watcher_identity()
    assert identity is not None
    assert identity["agent_uuid"] == "uuid"


def test_watcher_resume_path_unaffected_by_guard(
    watcher_module, monkeypatch, tmp_path
):
    """When anchor is present with agent_uuid, PATH 0 resume runs regardless
    of UNITARES_FIRST_RUN — the guard only fires on the fresh-onboard branch."""
    import json

    session_file = tmp_path / "watcher.json"
    session_file.write_text(json.dumps({"agent_uuid": "uuid"}))
    monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    monkeypatch.setattr(watcher_module, "_watcher_identity", None)

    client = _MockClient()
    watcher_module.resolve_identity(client)

    assert client.identity_calls == 1
    assert client.onboard_calls == 0


# ── #727: token-less UDS anchor must still attempt PATH 0 resume ──
#
# A substrate-enrolled resident under UNITARES_UDS_SOCKET deliberately writes
# a token-less ("uuid_only") anchor. The server's S19 PR3e gate authorizes the
# resume via kernel-attested peer match, so the client MUST attempt the
# agent_uuid-direct resume even without a continuity_token. The pre-fix code
# gated the attempt on token presence and skipped PATH 0 entirely, leaving an
# enrolled resident permanently stuck-down.


class _MockClientNoToken:
    """Mock client that mirrors a fresh UDS connection: no pre-set token."""

    client_session_id = "sess"
    continuity_token = None
    agent_uuid = "uuid"

    def __init__(self):
        self.onboard_calls = 0
        self.identity_calls = 0
        self.last_identity_kwargs = None

    def onboard(self, *a, **kw):
        self.onboard_calls += 1
        return type("R", (), {"success": True})()

    def identity(self, **kw):
        self.identity_calls += 1
        self.last_identity_kwargs = kw
        return type("R", (), {"success": True})()


def test_watcher_resumes_token_less_anchor_under_uds(
    watcher_module, monkeypatch, tmp_path
):
    """Token-less anchor + UDS-anchored → attempt PATH 0 resume WITHOUT a
    continuity_token, relying on the server's substrate peer-attestation gate."""
    import json

    session_file = tmp_path / "watcher.json"
    session_file.write_text(json.dumps({"agent_uuid": "uuid"}))
    monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    monkeypatch.setenv("UNITARES_UDS_SOCKET", str(tmp_path / "gov.sock"))
    monkeypatch.setattr(watcher_module, "_watcher_identity", None)

    client = _MockClientNoToken()
    watcher_module.resolve_identity(client)

    assert client.identity_calls == 1
    assert client.onboard_calls == 0
    # Attempted token-free — no continuity_token in the call.
    assert "continuity_token" not in client.last_identity_kwargs
    assert client.last_identity_kwargs["agent_uuid"] == "uuid"
    assert client.last_identity_kwargs["resume"] is True


def test_watcher_skips_token_less_anchor_without_uds(
    watcher_module, monkeypatch, tmp_path
):
    """Token-less anchor + NOT UDS-anchored → no proof channel, so PATH 0 is
    skipped (no pointless round-trip) and the fresh-onboard guard refuses."""
    import json

    session_file = tmp_path / "watcher.json"
    session_file.write_text(json.dumps({"agent_uuid": "uuid"}))
    monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    monkeypatch.setattr(watcher_module, "_watcher_identity", None)

    client = _MockClientNoToken()
    watcher_module.resolve_identity(client)

    assert client.identity_calls == 0
    assert client.onboard_calls == 0
    assert watcher_module.get_watcher_identity() is None
