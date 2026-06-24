"""Orchestrated-reviewer dispatch (the agent-orchestrator's first consumer).

Design (b): in-process synthetic reviewer is the default + fallback; orchestrated
dispatch is opt-in and degrades to None (→ in-process) on any failure.
"""
import json
import sys
import pytest
from unittest.mock import patch

from src.mcp_handlers.dialectic import orchestrator_dispatch as od


# --------------------------- the opt-in gate --------------------------- #

@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("on", True), ("YES", True),
    ("0", False), ("", False), ("off", False), ("no", False),
])
def test_gate(val, expected, monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_ORCHESTRATED_REVIEW", val)
    assert od.orchestrated_review_enabled() is expected


def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_ORCHESTRATED_REVIEW", raising=False)
    assert od.orchestrated_review_enabled() is False


# --------------------------- spec translation --------------------------- #

def test_build_spec_marshals_thesis_and_paths():
    spec = od._build_spec(
        "sess-1",
        {"root_cause": "rc", "proposed_conditions": ["a", "b"], "reasoning": "why"},
        "parent-uuid",
    )
    assert spec["cmd"] == sys.executable
    assert spec["args"] == ["-m", "agents.dialectic_reviewer"]
    env = spec["env"]
    assert env["DIALECTIC_SESSION_ID"] == "sess-1"
    assert env["DIALECTIC_THESIS_ROOT_CAUSE"] == "rc"
    assert json.loads(env["DIALECTIC_THESIS_CONDITIONS"]) == ["a", "b"]
    assert env["DIALECTIC_THESIS_REASONING"] == "why"
    assert env["UNITARES_PARENT_AGENT_ID"] == "parent-uuid"
    # PYTHONPATH must let the spawned process import both packages.
    assert "agents/sdk/src" in env["PYTHONPATH"]
    assert str(od._REPO_ROOT) in env["PYTHONPATH"]


def test_build_spec_omits_parent_when_absent():
    spec = od._build_spec("s", {"root_cause": "", "proposed_conditions": [], "reasoning": ""}, None)
    assert "UNITARES_PARENT_AGENT_ID" not in spec["env"]
    assert json.loads(spec["env"]["DIALECTIC_THESIS_CONDITIONS"]) == []


# --------------------------- dispatch (mocked HTTP) --------------------------- #

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp, *, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc
        self.posted = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if self._raise:
            raise self._raise
        self.posted = {"url": url, "json": json, "headers": headers}
        return self._resp


def _patch_httpx(monkeypatch, client):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)


@pytest.mark.asyncio
async def test_dispatch_returns_none_without_bearer(monkeypatch):
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    out = await od.dispatch_orchestrated_review("s", {"root_cause": "x"}, None)
    assert out is None


@pytest.mark.asyncio
async def test_dispatch_success_returns_payload(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    # The orchestrator's real success shape is {"ok": true, "agent_id": ...}.
    client = _FakeClient(_FakeResp(201, {"ok": True, "agent_id": "agent-xyz", "protocol_version": "v0.1"}))
    _patch_httpx(monkeypatch, client)

    out = await od.dispatch_orchestrated_review(
        "sess-9", {"root_cause": "rc", "proposed_conditions": ["c"], "reasoning": "r"}, "parent"
    )
    assert out["agent_id"] == "agent-xyz"
    # bearer + spec actually went on the wire
    assert client.posted["headers"]["Authorization"] == "Bearer tok"
    assert client.posted["json"]["env"]["DIALECTIC_SESSION_ID"] == "sess-9"
    assert client.posted["url"].endswith("/v1/agents")


@pytest.mark.asyncio
async def test_dispatch_non_2xx_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(_FakeResp(503, {"error": "service_unavailable"})))
    out = await od.dispatch_orchestrated_review("s", {"root_cause": "x"}, None)
    assert out is None


@pytest.mark.asyncio
async def test_dispatch_exception_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(None, raise_exc=RuntimeError("conn refused")))
    out = await od.dispatch_orchestrated_review("s", {"root_cause": "x"}, None)
    assert out is None
