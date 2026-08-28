"""Tests for the agent-callable long-running delegation lane (Claude, Codex)."""

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
    monkeypatch.setattr(
        di,
        "get_context_resolved_agent_id",
        lambda: "uuid-requester",
    )
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
                "terminal_answer": {
                    "schema": "unitares.terminal_answer.v1",
                    "status": "complete",
                },
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
    assert inference["terminal_answer"] == {
        "schema": "unitares.terminal_answer.v1",
        "status": "complete",
    }
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
    assert parsed["execution_started"] is False
    assert parsed["possibly_running"] is False
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
    assert parsed["execution_started"] is True
    assert parsed["possibly_running"] is True
    assert parsed["recovery"]["action"].startswith("Do not retry yet.")
    assert "reconcile or terminate" in parsed["recovery"]["action"]


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


@pytest.mark.asyncio
async def test_explicit_spawn_rejection_is_pre_execution(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "error": "spawn 503",
            "dispatch_phase": "spawn_rejected",
            "provenance": {},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert outcome.ok is False
    assert outcome.failure is not None
    assert outcome.failure.code == "INFERENCE_HOST_UNAVAILABLE"
    assert outcome.failure.execution_started is False
    assert outcome.failure.possibly_running is False


@pytest.mark.asyncio
async def test_acknowledged_spawn_without_id_is_possibly_running(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "error": "spawn returned no agent_id",
            "dispatch_phase": "spawn_acknowledged",
            "provenance": {},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert outcome.ok is False
    assert outcome.failure is not None
    assert outcome.failure.code == "DELEGATED_INFERENCE_FAILED"
    assert outcome.failure.execution_started is True
    assert outcome.failure.possibly_running is True
    assert outcome.failure.details["dispatch_phase"] == "spawn_acknowledged"
    assert outcome.failure.details["execution_started"] is True
    assert outcome.failure.details["possibly_running"] is True


@pytest.mark.asyncio
async def test_success_without_validated_terminal_answer_fails_closed(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": True,
            "text": "I will inspect the repository next.",
            "agent_id": "orch-agent-plan",
            "exit_status": 0,
            "provenance": {"finish_reason": "success"},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert outcome.ok is False
    assert outcome.failure is not None
    assert outcome.failure.code == "DELEGATED_INFERENCE_FAILED"
    assert outcome.failure.execution_started is True
    assert outcome.failure.possibly_running is False
    assert outcome.failure.details["adapter_status"] == "malformed"
    assert outcome.failure.details["execution_started"] is True
    assert outcome.failure.details["possibly_running"] is False


@pytest.mark.asyncio
async def test_raw_handler_preserves_adapter_failure_error_code(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "error": "spawn 503",
            "dispatch_phase": "spawn_rejected",
            "provenance": {},
        }),
    )

    parsed = _payload(await di.handle_delegate_inference({"prompt": "review"}))

    assert parsed["success"] is False
    assert parsed["error_code"] == "DELEGATED_INFERENCE_FAILED"
    assert "dispatch_phase" not in parsed


async def _async_value(value):
    return value


def _codex_host(*, configured=True, available=True, accepts=None):
    return {
        "host_id": "codex:host-adapter",
        "display_name": "Codex host adapter",
        "provider_kind": "codex_host_adapter",
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


def test_schema_accepts_the_codex_host_id():
    """The Literal was the hard gate — the service below it is host-agnostic."""
    from pydantic import ValidationError

    from src.mcp_handlers.schemas.core import DelegateInferenceParams

    assert DelegateInferenceParams(
        prompt="hi", host_id="codex:host-adapter"
    ).host_id == "codex:host-adapter"
    # Claude stays the default: its CLI reports exact model ids, Codex's does not.
    assert DelegateInferenceParams(prompt="hi").host_id == "claude:host-adapter"
    with pytest.raises(ValidationError):
        DelegateInferenceParams(prompt="hi", host_id="ollama:local")


@pytest.mark.asyncio
async def test_delegate_inference_routes_to_codex(monkeypatch):
    """A codex call reaches the adapter and is reported as its own host.

    The Codex CLI reports no model identifier, so provenance carries the
    warning instead of a model id — the evidence must not imply otherwise.
    """
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _codex_host())
    monkeypatch.setattr(di, "get_context_resolved_agent_id", lambda: "uuid-requester")
    captured = {}

    async def fake_invoke(host_id, prompt, **kwargs):
        captured.update({"host_id": host_id, "prompt": prompt, **kwargs})
        return {
            "ok": True,
            "host_id": host_id,
            "text": "codex answer",
            "exit_status": 0,
            "agent_id": "orch-agent-2",
            "provenance": {
                "models_used": [],
                "latency_ms": 900,
                "warnings": ["CLI did not report an exact model identifier"],
                "terminal_answer": {
                    "schema": "unitares.terminal_answer.v1",
                    "status": "complete",
                },
            },
        }

    async def fake_track(agent_uuid, **kwargs):
        return None

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    monkeypatch.setattr(di, "_track_energy", fake_track)

    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "review this diff",
        "host_id": "codex:host-adapter",
        "model": "gpt-5-codex",
        "task_type": "review",
    }))

    assert parsed["success"] is True
    assert parsed["response"] == "codex answer"
    assert captured["host_id"] == "codex:host-adapter"
    assert captured["model"] == "gpt-5-codex"
    inference = parsed["inference"]
    assert inference["host_id"] == "codex:host-adapter"
    assert inference["provider_kind"] == "codex_host_adapter"
    assert inference["model_requested"] == "gpt-5-codex"
    assert inference["model_used"] is None
    assert inference["models_used"] == []
    # The message named Claude on every host before Codex was wired.
    assert "Codex" in parsed["message"]
    assert "Claude" not in parsed["message"]


@pytest.mark.asyncio
async def test_unavailable_codex_names_its_own_cli_variable(monkeypatch):
    """Recovery text pointed at UNITARES_CLAUDE_CLI whichever host failed."""
    monkeypatch.setattr(
        di, "get_inference_host", lambda _host_id: _codex_host(available=False)
    )
    monkeypatch.setattr(di, "get_context_resolved_agent_id", lambda: "uuid-requester")

    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "review this diff",
        "host_id": "codex:host-adapter",
    }))

    assert parsed["success"] is False
    action = parsed["recovery"]["action"]
    assert "UNITARES_CODEX_CLI" in action
    assert "UNITARES_CLAUDE_CLI" not in action
