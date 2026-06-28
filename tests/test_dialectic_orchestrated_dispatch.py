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


def test_build_spec_does_not_forward_beam_flag(monkeypatch):
    """The reviewer submits via the gov-mcp `dialectic` tool, so its writes run in
    gov-mcp (where the flag already applies) — the spawn env must NOT carry
    UNITARES_DIALECTIC_BEAM_RESOLUTION / lease creds (reverts #1185's no-op
    forwarding). Even with them set in the parent env, the spec omits them."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok-xyz")
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788")
    spec = od._build_spec("s", {"root_cause": "", "proposed_conditions": [], "reasoning": ""}, None)
    env = spec["env"]
    assert "UNITARES_DIALECTIC_BEAM_RESOLUTION" not in env
    assert "LEASE_PLANE_BEARER_TOKEN" not in env
    assert "LEASE_PLANE_BASE_URL" not in env


@pytest.mark.parametrize("env_val,expected", [
    (None, "http://127.0.0.1:8767/mcp/"),                       # default has the path
    ("http://127.0.0.1:8767", "http://127.0.0.1:8767/mcp/"),    # bare base → append
    ("http://127.0.0.1:8767/", "http://127.0.0.1:8767/mcp/"),   # trailing slash only
    ("http://host:9000/mcp/", "http://host:9000/mcp/"),         # full mcp_url untouched
])
def test_governance_url_normalizes_to_mcp_path(env_val, expected, monkeypatch):
    """The reviewer passes this to GovernanceClient(mcp_url=...) which needs /mcp/.
    A bare base URL made session.initialize() hang (live-found 2026-06-23)."""
    monkeypatch.delenv("GOVERNANCE_URL", raising=False)
    if env_val is None:
        monkeypatch.delenv("UNITARES_GOVERNANCE_URL", raising=False)
    else:
        monkeypatch.setenv("UNITARES_GOVERNANCE_URL", env_val)
    assert od._governance_url() == expected


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


# --------------------------- reviewer_crashed_fast --------------------------- #

@pytest.mark.asyncio
async def test_crashed_fast_true_on_nonzero_exit(monkeypatch):
    """Reviewer exited non-zero within the window → crashed → caller falls back."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(_FakeResp(200, {"ok": True, "result": {"exit_status": 1}})))
    assert await od.reviewer_crashed_fast("ag-1", await_seconds=0.01) is True


@pytest.mark.asyncio
async def test_crashed_fast_false_on_clean_exit(monkeypatch):
    """Reviewer exited 0 (submitted its verdict) → not a crash → async path owns it."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(_FakeResp(200, {"ok": True, "result": {"exit_status": 0}})))
    assert await od.reviewer_crashed_fast("ag-2", await_seconds=0.01) is False


@pytest.mark.asyncio
async def test_crashed_fast_false_on_await_timeout(monkeypatch):
    """504 await_timeout = still running (gemma4 working) → leave on async path."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(_FakeResp(504, {"ok": False, "error": "await_timeout"})))
    assert await od.reviewer_crashed_fast("ag-3", await_seconds=0.01) is False


@pytest.mark.asyncio
async def test_crashed_fast_false_on_error_or_no_bearer(monkeypatch):
    """Can't tell (exception / no bearer) ⇒ don't double-run; leave it to the reviewer."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    _patch_httpx(monkeypatch, _FakeClient(None, raise_exc=RuntimeError("boom")))
    assert await od.reviewer_crashed_fast("ag-4", await_seconds=0.01) is False
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    assert await od.reviewer_crashed_fast("ag-5", await_seconds=0.01) is False
