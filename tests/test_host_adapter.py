"""Tests for the strong-heterogeneous inference host adapter (orchestrator-backed)."""

import asyncio
import json

from src.mcp_handlers.support import host_adapter as ha


def _run(coro):
    return asyncio.run(coro)


def test_enabled_flag(monkeypatch):
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    assert ha.host_adapter_enabled() is False
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    assert ha.host_adapter_enabled() is True


def test_available_requires_flag_cli_and_bearer(monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    assert ha.host_adapter_available("codex:host-adapter") is True

    # flag off -> unavailable
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    assert ha.host_adapter_available("codex:host-adapter") is False

    # cli absent -> unavailable
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setattr(ha.shutil, "which", lambda c: None)
    assert ha.host_adapter_available("codex:host-adapter") is False

    # bearer absent -> unavailable
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    assert ha.host_adapter_available("codex:host-adapter") is False

    # unknown host -> unavailable
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    assert ha.host_adapter_available("nope:host-adapter") is False


def test_extract_text_codex_strips_marker_and_footer():
    out = ["warning: noise", "codex", "answer line 1", "answer line 2", "tokens used", "1234"]
    assert ha._extract_text(out, family="openai_codex") == "answer line 1\nanswer line 2"


def test_extract_text_non_codex_passthrough():
    out = ["the claude answer", "second line"]
    assert ha._extract_text(out, family="anthropic_claude") == "the claude answer\nsecond line"


def test_invoke_disabled(monkeypatch):
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi"))
    assert r["ok"] is False and "disabled" in r["error"]


def test_invoke_unknown_host(monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    r = _run(ha.invoke_host_adapter("nope", "hi"))
    assert r["ok"] is False and "unknown host adapter" in r["error"]


def test_invoke_bearer_missing(monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi"))
    assert r["ok"] is False and "BEARER" in r["error"].upper()


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _patch_httpx(monkeypatch, responses):
    """Patch httpx.AsyncClient so successive .post() calls (spawn, then await)
    return `responses` in order, across the two AsyncClient instantiations."""
    state = {"i": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            resp = responses[state["i"]]
            state["i"] += 1
            return resp

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())


def _enable(monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)


def test_invoke_happy_path(monkeypatch):
    _enable(monkeypatch)
    _patch_httpx(monkeypatch, [
        _FakeResp(201, {"ok": True, "agent_id": "ag-1"}),
        _FakeResp(200, {"result": {"exit_status": 0, "output": ["codex", "ANSWER", "tokens used", "9"]}}),
    ])
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi", timeout_s=5))
    assert r["ok"] is True
    assert r["text"] == "ANSWER"
    assert r["agent_id"] == "ag-1"
    assert r["exit_status"] == 0
    assert r["provenance"]["model_family"] == "openai_codex"
    assert r["provenance"]["transport"] == "host_adapter"


def test_invoke_still_running_on_await_timeout(monkeypatch):
    _enable(monkeypatch)
    _patch_httpx(monkeypatch, [
        _FakeResp(201, {"ok": True, "agent_id": "ag-2"}),
        _FakeResp(504, {}),
    ])
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi", timeout_s=1))
    assert r["ok"] is False
    assert r["status"] == "still_running"
    assert r["agent_id"] == "ag-2"


def test_invoke_nonzero_exit(monkeypatch):
    _enable(monkeypatch)
    _patch_httpx(monkeypatch, [
        _FakeResp(201, {"ok": True, "agent_id": "ag-3"}),
        _FakeResp(200, {"result": {"exit_status": 1, "output": ["boom"]}}),
    ])
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi", timeout_s=5))
    assert r["ok"] is False
    assert r["exit_status"] == 1


def test_invoke_spawn_failure(monkeypatch):
    _enable(monkeypatch)
    _patch_httpx(monkeypatch, [_FakeResp(500, {"error": "boom"})])
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi", timeout_s=5))
    assert r["ok"] is False
    assert "spawn 500" in r["error"]


def test_registry_reflects_availability(monkeypatch):
    """The registry codex/claude records flip available with the flag/CLI/bearer."""
    from src.mcp_handlers.support import inference_registry as reg

    _enable(monkeypatch)
    hosts = {h["host_id"]: h for h in reg.list_inference_hosts()}
    assert hosts["codex:host-adapter"]["available"] is True
    assert hosts["codex:host-adapter"]["implementation_status"] == "active"

    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    hosts2 = {h["host_id"]: h for h in reg.list_inference_hosts()}
    assert hosts2["codex:host-adapter"]["available"] is False
    assert hosts2["codex:host-adapter"]["implementation_status"] == "opt_in"
