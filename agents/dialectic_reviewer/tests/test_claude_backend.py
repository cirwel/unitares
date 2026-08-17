"""Claude dialectic backend safety and provenance contract."""

import asyncio
import json

from agents.dialectic_reviewer import host_backends as hb


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        self.killed = True


def test_claude_backend_disables_tools_and_preserves_provenance(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_CLAUDE_MODEL", "claude-opus-5")
    monkeypatch.setattr(
        hb,
        "resolve_host_cli",
        lambda _host_id: "/Users/operator/.local/bin/claude",
    )
    provider_result = {
        "subtype": "success",
        "result": json.dumps({
            "agrees": False,
            "root_cause": "missing evidence",
            "proposed_conditions": ["add evidence"],
            "reasoning": "the thesis is under-supported",
        }),
        "total_cost_usd": 0.025,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {
            "claude-opus-5": {"inputTokens": 10, "outputTokens": 20},
        },
    }
    captured = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(json.dumps(provider_result).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    result = asyncio.run(hb.call_claude_backend("review this"))

    assert result.text is not None
    assert result.model_used == "claude-opus-5"
    assert result.models_used == ["claude-opus-5"]
    assert result.tokens_used == 30
    assert result.cost_usd == 0.025
    shell_command = captured["args"][2]
    assert "--safe-mode" in shell_command
    assert '--tools ""' in shell_command
    assert "--no-session-persistence" in shell_command
    assert "--output-format json" in shell_command
    assert '"$DR_MODEL"' in shell_command
    child_env = captured["kwargs"]["env"]
    assert child_env["DR_PROMPT"] == "review this"
    assert child_env["DR_MODEL"] == "claude-opus-5"
    assert child_env["DR_CLI"].endswith("/.local/bin/claude")


def test_claude_backend_absent_cli_returns_structured_failure(monkeypatch):
    monkeypatch.setattr(hb, "resolve_host_cli", lambda _host_id: None)
    result = asyncio.run(hb.call_claude_backend("review this"))
    assert result.text is None
    assert result.host_id == "claude:host-adapter"
    assert "not found" in (result.error or "")


def test_claude_backend_spawn_failure_returns_structured_failure(monkeypatch):
    monkeypatch.setattr(hb, "resolve_host_cli", lambda _host_id: "/bin/claude")

    async def fail_spawn(*_args, **_kwargs):
        raise OSError("exec unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    result = asyncio.run(hb.call_claude_backend("review this"))
    assert result.text is None
    assert result.error == "Claude CLI spawn failed: OSError"


def test_claude_backend_timeout_kills_child_and_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S", "2")
    monkeypatch.setattr(hb, "resolve_host_cli", lambda _host_id: "/bin/claude")
    process = _FakeProcess(b"")

    async def fake_spawn(*_args, **_kwargs):
        return process

    original_wait_for = asyncio.wait_for
    calls = 0

    async def timeout_once(awaitable, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            awaitable.close()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(asyncio, "wait_for", timeout_once)
    result = asyncio.run(hb.call_claude_backend("review this"))
    assert process.killed is True
    assert result.text is None
    assert "exceeded 2s" in (result.error or "")


def test_claude_backend_rejects_non_verdict_answer(monkeypatch):
    monkeypatch.setattr(hb, "resolve_host_cli", lambda _host_id: "/bin/claude")
    provider_result = {
        "subtype": "success",
        "result": "I cannot decide.",
        "usage": {},
        "modelUsage": {"claude-sonnet-4-5": {}},
    }

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProcess(json.dumps(provider_result).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    result = asyncio.run(hb.call_claude_backend("review this"))
    assert result.text is None
    assert result.models_used == ["claude-sonnet-4-5"]
    assert "no parseable" in (result.error or "")


def test_claude_backend_rejects_provider_declared_error(monkeypatch):
    monkeypatch.setattr(hb, "resolve_host_cli", lambda _host_id: "/bin/claude")
    provider_result = {
        "subtype": "error_during_execution",
        "is_error": True,
        "result": json.dumps({
            "agrees": True,
            "root_cause": "untrusted error payload",
            "proposed_conditions": ["ignore"],
            "reasoning": "must not become a verdict",
        }),
        "usage": {"output_tokens": 5},
        "modelUsage": {"claude-sonnet-4-5": {}},
    }

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProcess(json.dumps(provider_result).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    result = asyncio.run(hb.call_claude_backend("review this"))
    assert result.text is None
    assert result.models_used == ["claude-sonnet-4-5"]
    assert result.tokens_used == 5
    assert result.error == "Claude CLI reported an error result"
