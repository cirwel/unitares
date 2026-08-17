"""Tests for the agent-callable long-running Claude delegation lane."""

import json

import pytest

from src.mcp_handlers.decorators import get_call_identity_requirement
from src.mcp_handlers.support import delegated_inference as di


def _payload(result):
    return json.loads(result[0].text)


def _claude_host(*, configured=True, available=True, accepts=None):
    return {
        "host_id": "claude:host-adapter",
        "provider_kind": "claude_host_adapter",
        "transport": "host_adapter",
        "configured": configured,
        "available": available,
        "privacy_class": "operator_authorized_external",
        "cost_class": "subscription_backed",
        "accountability_class": "tool_evidence",
        "accepts_host_id_from": (
            ["delegate_inference"] if accepts is None else accepts
        ),
    }


def test_delegate_inference_requires_attributed_identity():
    assert get_call_identity_requirement("delegate_inference", {}) == "required"


@pytest.mark.asyncio
async def test_delegate_inference_returns_attributed_provenance(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(di, "get_context_agent_id", lambda: "uuid-requester")
    captured = {}

    async def fake_invoke(host_id, prompt, **kwargs):
        captured.update({"host_id": host_id, "prompt": prompt, **kwargs})
        return {
            "ok": True,
            "host_id": host_id,
            "text": "careful answer",
            "raw": "provider envelope must not escape",
            "exit_status": 0,
            "agent_id": "orch-agent-1",
            "provenance": {
                "model_used": None,
                "models_used": ["claude-haiku-4-5", "claude-opus-5"],
                "tokens_used": 41,
                "cost_usd": 0.02,
                "latency_ms": 1200,
                "finish_reason": "success",
                "provider_usage": {"input_tokens": 10, "output_tokens": 31},
                "provider_model_usage": {"claude-opus-5": {"outputTokens": 31}},
                "warnings": ["multiple models"],
            },
        }

    tracked = {}

    async def fake_track(agent_uuid, **kwargs):
        tracked.update({"agent_uuid": agent_uuid, **kwargs})

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    monkeypatch.setattr(di, "_track_energy", fake_track)

    result = await di.handle_delegate_inference({
        "prompt": "challenge this thesis",
        "model": "claude-opus-5",
        "task_type": "review",
        "timeout_s": 90,
    })
    parsed = _payload(result)

    assert parsed["success"] is True
    assert parsed["response"] == "careful answer"
    assert parsed["models_used"] == ["claude-haiku-4-5", "claude-opus-5"]
    assert "raw" not in parsed
    assert captured == {
        "host_id": "claude:host-adapter",
        "prompt": "challenge this thesis",
        "timeout_s": 90,
        "sandbox": "read-only",
        "model": "claude-opus-5",
    }
    inference = parsed["inference"]
    assert inference["requesting_agent_uuid"] == "uuid-requester"
    assert inference["orchestrator_agent_id"] == "orch-agent-1"
    assert inference["model_requested"] == "claude-opus-5"
    assert inference["model_used"] is None
    assert inference["cost_usd"] == 0.02
    assert inference["prompt_hash"].startswith("sha256:")
    assert inference["response_hash"].startswith("sha256:")
    assert tracked["agent_uuid"] == "uuid-requester"
    assert tracked["tokens_used"] == 41


@pytest.mark.asyncio
async def test_delegate_inference_fails_closed_when_adapter_unavailable(monkeypatch):
    monkeypatch.setattr(
        di,
        "get_inference_host",
        lambda _host_id: _claude_host(configured=True, available=False),
    )
    result = await di.handle_delegate_inference({"prompt": "hello"})
    parsed = _payload(result)
    assert parsed["success"] is False
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert "UNITARES_CLAUDE_CLI" in parsed["recovery"]["action"]


@pytest.mark.asyncio
async def test_delegate_inference_rejects_unwired_host(monkeypatch):
    codex_host = _claude_host(accepts=[])
    codex_host["host_id"] = "codex:host-adapter"
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: codex_host)
    result = await di.handle_delegate_inference({
        "prompt": "hello",
        "host_id": "codex:host-adapter",
    })
    parsed = _payload(result)
    assert parsed["success"] is False
    assert parsed["error_code"] == "INFERENCE_HOST_UNREACHABLE"


@pytest.mark.asyncio
async def test_delegate_inference_surfaces_orchestrator_timeout(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())

    async def fake_invoke(*_args, **_kwargs):
        return {
            "ok": False,
            "status": "still_running",
            "agent_id": "orch-agent-timeout",
            "provenance": {"transport": "host_adapter"},
        }

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    result = await di.handle_delegate_inference({
        "prompt": "hard problem",
        "timeout_s": 5,
    })
    parsed = _payload(result)
    assert parsed["success"] is False
    assert parsed["error_code"] == "DELEGATED_INFERENCE_TIMEOUT"
    assert parsed["orchestrator_agent_id"] == "orch-agent-timeout"


@pytest.mark.asyncio
async def test_delegate_inference_rejects_unknown_host(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: None)
    result = await di.handle_delegate_inference({
        "prompt": "hello",
        "host_id": "unknown:host",
    })
    parsed = _payload(result)
    assert parsed["success"] is False
    assert parsed["error_code"] == "INFERENCE_HOST_NOT_FOUND"
