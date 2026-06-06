"""Watcher check-in must carry the continuity token for proof-based rebind.

Regression coverage for the "Identity not resolved" outage: the Watcher's
session->uuid binding lives only in Redis with a ~24h TTL and is never
DB-persisted (REST resolution uses persist=False). A check-in that sends only
client_session_id has nothing to rebind from once that cache entry lapses, so
process_agent_update returns "Identity not resolved" (observed 2026-06-05:
failures began exactly 24h after the last good check-in). Passing the
continuity token routes process_agent_update through the REST PATH 2.8
cryptographic-ownership rebind, which re-warms the cache and self-heals.
"""

from __future__ import annotations

import agents.watcher.agent as agent


class _FakeClient:
    def __init__(self):
        self.client_session_id = None
        self.continuity_token = None
        self.agent_uuid = None
        self.checkin_kwargs = None

    def checkin(self, **kwargs):
        self.checkin_kwargs = kwargs
        return {"verdict": "proceed"}


def _wire(monkeypatch, identity):
    fake = _FakeClient()
    monkeypatch.setattr(agent, "_make_identity_client", lambda: fake)
    monkeypatch.setattr(agent, "get_watcher_identity", lambda: identity)
    monkeypatch.setattr(agent, "_build_checkin_summary", lambda: ("summary", 0.2, 0.7))
    return fake


def test_checkin_forwards_continuity_token(monkeypatch):
    identity = {
        "client_session_id": "agent-907e3195-c64",
        "continuity_token": "v1.sometoken",
        "agent_uuid": "907e3195-c649-49db-b753-1edc1a105f33",
    }
    fake = _wire(monkeypatch, identity)

    agent._do_checkin()

    assert fake.checkin_kwargs is not None
    assert fake.checkin_kwargs.get("continuity_token") == "v1.sometoken"
    # session fields are still restored onto the client for SDK injection
    assert fake.client_session_id == "agent-907e3195-c64"
    assert fake.agent_uuid == "907e3195-c649-49db-b753-1edc1a105f33"


def test_checkin_omits_token_when_absent(monkeypatch):
    identity = {
        "client_session_id": "agent-907e3195-c64",
        "continuity_token": "",
        "agent_uuid": "907e3195-c649-49db-b753-1edc1a105f33",
    }
    fake = _wire(monkeypatch, identity)

    agent._do_checkin()

    # An empty token must not be forwarded — sending "" would be a no-op at best
    # and a malformed-proof signal at worst.
    assert fake.checkin_kwargs is not None
    assert "continuity_token" not in fake.checkin_kwargs


def test_checkin_skipped_without_identity(monkeypatch):
    fake = _wire(monkeypatch, None)

    agent._do_checkin()

    assert fake.checkin_kwargs is None
