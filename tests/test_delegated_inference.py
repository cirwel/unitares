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
            "execution_id": "orch-execution-1",
            "agent_id": "orch-execution-1",
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
    assert inference["orchestrator_execution_id"] == "orch-execution-1"
    assert inference["orchestrator_agent_id"] == "orch-execution-1"
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
            "execution_id": "orch-execution-timeout",
            "agent_id": "orch-execution-timeout",
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
    assert parsed["orchestrator_execution_id"] == "orch-execution-timeout"
    assert parsed["orchestrator_agent_id"] == "orch-execution-timeout"
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
            "error": "spawn returned no execution_id or legacy agent_id",
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


@pytest.mark.asyncio
async def test_envelope_failure_carries_a_raw_excerpt(monkeypatch):
    """A malformed envelope must say what arrived, or it cannot be diagnosed."""
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "status": "malformed",
            "text": "",
            "raw": '```json\n{"schema":"x","status":"complete","answer":"hi"}\n```',
            "agent_id": "orch-agent-fenced",
            "exit_status": 0,
            "error": "Host CLI returned a malformed terminal answer envelope",
            "provenance": {"finish_reason": "success"},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert outcome.ok is False
    details = outcome.failure.details
    # Still fails closed — the excerpt adds diagnosis, never tolerance.
    assert details["adapter_status"] == "malformed"
    assert "```json" in details["adapter_raw_excerpt"]
    assert details["adapter_raw_excerpt_truncated"] is False
    assert "NOT parsed" in details["adapter_raw_excerpt_note"]


@pytest.mark.asyncio
async def test_raw_excerpt_is_bounded(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "status": "malformed",
            "text": "",
            "raw": "x" * (di._RAW_EXCERPT_LIMIT + 500),
            "agent_id": "orch-agent-flood",
            "exit_status": 0,
            "error": "Host CLI returned a malformed terminal answer envelope",
            "provenance": {},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    details = outcome.failure.details
    excerpt = details["adapter_raw_excerpt"]
    assert details["adapter_raw_excerpt_truncated"] is True
    assert di._RAW_EXCERPT_LIMIT <= len(excerpt) <= di._RAW_EXCERPT_LIMIT + 64


@pytest.mark.asyncio
async def test_no_raw_excerpt_on_a_complete_answer(monkeypatch):
    """A successful envelope needs no excerpt; the answer is the answer."""
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "status": "complete",
            "text": "answered",
            "raw": "some raw transcript",
            "agent_id": "orch-agent-complete",
            "exit_status": 1,
            "error": "Host adapter returned a nonzero exit",
            "provenance": {},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert "adapter_raw_excerpt" not in outcome.failure.details


async def _codex_failure_details(monkeypatch, raw):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _codex_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "status": "malformed",
            "text": "",
            "raw": raw,
            "agent_id": "orch-agent-codex",
            "exit_status": 0,
            "error": "Host CLI returned a malformed terminal answer envelope",
            "provenance": {"model_family": "openai_codex"},
        }),
    )
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        host_id="codex:host-adapter",
        requesting_agent_uuid="requester",
    ))
    assert outcome.ok is False
    return outcome.failure.details


@pytest.mark.asyncio
async def test_truncated_excerpt_keeps_the_tail(monkeypatch):
    """The answer, or its absence, is at the END of a CLI transcript.

    A head-only excerpt spends the whole budget on the banner and the echoed
    prompt — the two regions guaranteed to say nothing about why the envelope
    failed.
    """
    raw = "BANNER_START" + ("x" * (di._RAW_EXCERPT_LIMIT + 500)) + "ANSWER_END"
    details = await _codex_failure_details(monkeypatch, raw)

    excerpt = details["adapter_raw_excerpt"]
    assert details["adapter_raw_excerpt_truncated"] is True
    assert excerpt.startswith("BANNER_START")
    assert excerpt.endswith("ANSWER_END")
    assert "elided" in excerpt
    assert "Head and tail" in details["adapter_raw_excerpt_note"]


# Shaped like a real `codex exec` transcript (see
# agents/dialectic_reviewer/tests/test_codex_backend.py): banner, echoed prompt,
# then a bare `codex` marker line immediately before the assistant turn.
_CODEX_TRANSCRIPT_WITH_ANSWER = """OpenAI Codex v0.151.0
--------
workdir: /tmp/x
model: gpt-5.6-sol
--------
user
Reply with exactly the word: alive

--- UNITARES response contract ---
Return exactly one JSON object and no Markdown fence or surrounding text:
{"schema":"unitares.terminal_answer.v1","status":"complete","answer":"your final answer"}
codex
I think the answer is alive.
tokens used
1,234
"""

_CODEX_TRANSCRIPT_NO_ANSWER = """OpenAI Codex v0.151.0
--------
workdir: /tmp/x
model: gpt-5.6-sol
--------
user
Reply with exactly the word: alive

--- UNITARES response contract ---
Return exactly one JSON object and no Markdown fence or surrounding text:
{"schema":"unitares.terminal_answer.v1","status":"complete","answer":"your final answer"}
"""

<<<<<<< HEAD
_CODEX_JSONL_WITH_ANSWER = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
    json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "not a valid terminal envelope"},
    }),
    json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 4, "output_tokens": 5},
    }),
])

=======
>>>>>>> origin/master

@pytest.mark.asyncio
async def test_codex_failure_reports_a_located_answer_region(monkeypatch):
    """Marker present: the model answered, and answered badly. Fix the prompt."""
    details = await _codex_failure_details(
        monkeypatch, _CODEX_TRANSCRIPT_WITH_ANSWER
    )

    assert details["adapter_status"] == "malformed"
    assert details["adapter_answer_region_located"] is True
    assert "adapter_answer_region_note" not in details


@pytest.mark.asyncio
<<<<<<< HEAD
async def test_codex_jsonl_failure_reports_a_located_answer_region(monkeypatch):
    """The typed agent_message event is the primary answer boundary."""
    details = await _codex_failure_details(monkeypatch, _CODEX_JSONL_WITH_ANSWER)

    assert details["adapter_status"] == "malformed"
    assert details["adapter_answer_region_located"] is True
    assert "adapter_answer_region_note" not in details


@pytest.mark.asyncio
=======
>>>>>>> origin/master
async def test_codex_failure_distinguishes_a_missing_answer_region(monkeypatch):
    """Marker absent: no assistant turn was found at all.

    Same `malformed` error as the case above, opposite fix — so the two must not
    reach the caller as one sentence.
    """
    details = await _codex_failure_details(
        monkeypatch, _CODEX_TRANSCRIPT_NO_ANSWER
    )

    assert details["adapter_status"] == "malformed"
    assert details["adapter_answer_region_located"] is False
    assert "NOT the model returning a bad envelope" in (
        details["adapter_answer_region_note"]
    )


@pytest.mark.asyncio
async def test_answer_region_signal_is_codex_only(monkeypatch):
    """`claude -p` prints the answer directly; it has no marker to look for."""
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(
        di,
        "invoke_host_adapter",
        lambda *_args, **_kwargs: _async_value({
            "ok": False,
            "status": "malformed",
            "text": "",
            "raw": "here is a plan, not an answer",
            "agent_id": "orch-agent-claude",
            "exit_status": 0,
            "error": "Host CLI returned a malformed terminal answer envelope",
            "provenance": {"model_family": "anthropic_claude"},
        }),
    )

    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="review",
        requesting_agent_uuid="requester",
    ))

    assert "adapter_answer_region_located" not in outcome.failure.details
