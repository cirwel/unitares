"""Tests for the strong-heterogeneous inference host adapter (orchestrator-backed)."""

import asyncio
import json

import pytest

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
    monkeypatch.setattr(ha, "_is_executable", lambda _path: False)
    assert ha.host_adapter_available("codex:host-adapter") is False

    # bearer absent -> unavailable
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    assert ha.host_adapter_available("codex:host-adapter") is False

    # unknown host -> unavailable
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    assert ha.host_adapter_available("nope:host-adapter") is False


def test_resolve_claude_cli_from_operator_override(monkeypatch):
    monkeypatch.setenv("UNITARES_CLAUDE_CLI", "/opt/operator/bin/claude")
    monkeypatch.setattr(
        ha,
        "_is_executable",
        lambda path: path == "/opt/operator/bin/claude",
    )
    monkeypatch.setattr(ha.shutil, "which", lambda _cli: None)
    assert ha.resolve_host_cli("claude:host-adapter") == "/opt/operator/bin/claude"


def test_resolve_claude_cli_from_user_local_bin(monkeypatch):
    monkeypatch.delenv("UNITARES_CLAUDE_CLI", raising=False)
    monkeypatch.setattr(ha.shutil, "which", lambda _cli: None)
    expected = str(ha.Path.home() / ".local" / "bin" / "claude")
    monkeypatch.setattr(ha, "_is_executable", lambda path: path == expected)
    assert ha.resolve_host_cli("claude:host-adapter") == expected


def test_extract_text_codex_strips_marker_and_footer():
    out = ["warning: noise", "codex", "answer line 1", "answer line 2", "tokens used", "1234"]
    assert ha._extract_text(out, family="openai_codex") == "answer line 1\nanswer line 2"


def test_extract_text_non_codex_passthrough():
    out = ["the claude answer", "second line"]
    assert ha._extract_text(out, family="anthropic_claude") == "the claude answer\nsecond line"


def test_extract_claude_json_preserves_exact_models_usage_and_cost():
    payload = {
        "subtype": "success",
        "result": "CLAUDE ANSWER",
        "total_cost_usd": 0.0318,
        "duration_api_ms": 923,
        "usage": {
            "input_tokens": 4,
            "output_tokens": 7,
            "cache_read_input_tokens": 11,
        },
        "modelUsage": {
            "claude-opus-5": {"inputTokens": 4, "outputTokens": 7},
            "claude-haiku-4-5-20251001": {"inputTokens": 1, "outputTokens": 2},
        },
    }
    text, metadata = ha._extract_cli_result(
        [json.dumps(payload)],
        family="anthropic_claude",
    )
    assert text == "CLAUDE ANSWER"
    assert metadata["models_used"] == [
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
    ]
    assert metadata["model_used"] is None
    assert metadata["tokens_used"] == 22
    assert metadata["cost_usd"] == 0.0318
    assert "multiple models" in metadata["warnings"][0]


def test_invoke_disabled(monkeypatch):
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    r = _run(ha.invoke_host_adapter("codex:host-adapter", "hi"))
    assert r["ok"] is False and "disabled" in r["error"]
    assert r["dispatch_phase"] == "preflight"


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
    state = {"i": 0, "calls": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            state["calls"].append((url, kw))
            resp = responses[state["i"]]
            state["i"] += 1
            return resp

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    return state


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


def test_invoke_claude_is_safe_and_model_is_env_quoted(monkeypatch):
    _enable(monkeypatch)
    claude_payload = {
        "subtype": "success",
        "result": "ANSWER",
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "modelUsage": {"claude-sonnet-4-5": {}},
    }
    state = _patch_httpx(monkeypatch, [
        _FakeResp(201, {"ok": True, "agent_id": "ag-claude"}),
        _FakeResp(200, {
            "result": {"exit_status": 0, "output": [json.dumps(claude_payload)]},
        }),
    ])

    r = _run(ha.invoke_host_adapter(
        "claude:host-adapter",
        "review this",
        timeout_s=5,
        model="claude-sonnet-4-5",
    ))

    spawn_spec = state["calls"][0][1]["json"]
    assert spawn_spec["env"]["HA_PROMPT"] == "review this"
    assert spawn_spec["env"]["HA_MODEL"] == "claude-sonnet-4-5"
    assert spawn_spec["env"]["HA_CLI"] == "/usr/bin/claude"
    shell_command = spawn_spec["args"][1]
    assert "--safe-mode" in shell_command
    assert '--tools ""' in shell_command
    assert "--no-session-persistence" in shell_command
    assert "--output-format json" in shell_command
    assert '"$HA_MODEL"' in shell_command
    assert r["text"] == "ANSWER"
    assert r["provenance"]["model_used"] == "claude-sonnet-4-5"
    assert r["provenance"]["models_used"] == ["claude-sonnet-4-5"]


def test_invoke_claude_provider_error_is_not_reported_as_success(monkeypatch):
    _enable(monkeypatch)
    claude_payload = {
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "provider failed",
        "usage": {},
        "modelUsage": {},
    }
    _patch_httpx(monkeypatch, [
        _FakeResp(201, {"ok": True, "agent_id": "ag-claude-error"}),
        _FakeResp(200, {
            "result": {"exit_status": 0, "output": [json.dumps(claude_payload)]},
        }),
    ])

    r = _run(ha.invoke_host_adapter(
        "claude:host-adapter",
        "review this",
        timeout_s=5,
    ))

    assert r["ok"] is False
    assert r["error"] == "Claude CLI reported an error result"
    assert r["provenance"]["provider_is_error"] is True


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
    assert r["dispatch_phase"] == "spawn_rejected"


def test_invoke_acknowledged_spawn_without_id_is_ambiguous(monkeypatch):
    _enable(monkeypatch)
    _patch_httpx(monkeypatch, [_FakeResp(201, {"ok": True})])

    result = _run(ha.invoke_host_adapter(
        "claude:host-adapter",
        "review",
        timeout_s=5,
    ))

    assert result["ok"] is False
    assert result["dispatch_phase"] == "spawn_acknowledged"
    assert "no agent_id" in result["error"]


def test_invoke_spawn_transport_error_is_ambiguous(monkeypatch):
    _enable(monkeypatch)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **_kwargs):
            raise RuntimeError("lost spawn acknowledgement")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    result = _run(ha.invoke_host_adapter(
        "claude:host-adapter",
        "review",
        timeout_s=5,
    ))

    assert result["ok"] is False
    assert result["dispatch_phase"] == "spawn_request_started"
    assert result.get("agent_id") is None


def test_invoke_exception_after_spawn_preserves_orchestrator_agent_id(monkeypatch):
    _enable(monkeypatch)
    state = {"calls": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **_kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                return _FakeResp(201, {"agent_id": "ag-preserved"})
            raise RuntimeError("await transport failed")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    result = _run(ha.invoke_host_adapter(
        "claude:host-adapter",
        "review",
        timeout_s=5,
    ))

    assert result["ok"] is False
    assert result["agent_id"] == "ag-preserved"
    assert result["dispatch_phase"] == "spawned"
    assert "await transport failed" in result["error"]


def test_cancellation_after_spawn_propagates_and_child_is_runtime_bounded(monkeypatch):
    _enable(monkeypatch)
    state = {"calls": 0, "spawn_spec": None}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                state["spawn_spec"] = kwargs["json"]
                return _FakeResp(201, {"agent_id": "ag-cancelled-await"})
            raise asyncio.CancelledError

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    with pytest.raises(asyncio.CancelledError):
        _run(ha.invoke_host_adapter(
            "claude:host-adapter",
            "review",
            timeout_s=5,
        ))

    # Cancellation never starts a fallback here. The orchestrator owns the
    # accepted child and its max-runtime backstop bounds an unpolled orphan.
    assert state["spawn_spec"]["max_runtime_ms"] == 35_000


def test_registry_reflects_availability(monkeypatch):
    """The registry codex/claude records flip available with the flag/CLI/bearer."""
    from src.mcp_handlers.support import inference_registry as reg

    _enable(monkeypatch)
    hosts = {h["host_id"]: h for h in reg.list_inference_hosts()}
    assert hosts["codex:host-adapter"]["available"] is True

    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    hosts2 = {h["host_id"]: h for h in reg.list_inference_hosts()}
    assert hosts2["codex:host-adapter"]["available"] is False


def test_only_claude_adapter_is_agent_callable(monkeypatch):
    """Availability is not callability.

    Reachability describes a routing contract and therefore does not flap with
    runtime readiness. Claude has delegate_inference; Codex remains unwired.
    """
    from src.mcp_handlers.support import inference_registry as reg

    for enabled in (True, False):
        if enabled:
            _enable(monkeypatch)
        else:
            monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
        hosts = {h["host_id"]: h for h in reg.list_inference_hosts()}
        assert hosts["codex:host-adapter"]["accepts_host_id_from"] == []
        assert hosts["codex:host-adapter"]["implementation_status"] == "built_unwired"
        assert hosts["claude:host-adapter"]["accepts_host_id_from"] == [
            "delegate_inference"
        ]
        assert hosts["claude:host-adapter"]["implementation_status"] == "active"

    # The synchronous hosts are the ones call_model can actually serve.
    hosts = {h["host_id"]: h for h in reg.list_inference_hosts()}
    assert hosts["ollama:local"]["accepts_host_id_from"] == ["call_model"]
    assert hosts["hf:router"]["accepts_host_id_from"] == ["call_model"]
