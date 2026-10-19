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
    # One overall deadline for the call: timeout_s plus the fixed overhead.
    deadline = captured.pop("deadline")
    assert isinstance(deadline, float)
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
async def test_a_switched_off_host_names_the_switch_not_the_setup(monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_DISABLED_HOSTS", "claude")
    monkeypatch.setattr(
        di,
        "get_inference_host",
        lambda _host_id: _claude_host(configured=True, available=False),
    )
    parsed = _payload(await di.handle_delegate_inference({"prompt": "hello"}))
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert parsed["possibly_running"] is False
    assert "UNITARES_HOST_ADAPTER_DISABLED_HOSTS" in parsed["recovery"]["action"]
    assert "UNITARES_CLAUDE_CLI" not in parsed["recovery"]["action"]


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
    """host_id is a bounded string; the handler gates it on the registry's
    accepts_host_id_from (test_delegate_inference_rejects_unwired_host)."""
    from pydantic import ValidationError

    from src.mcp_handlers.schemas.core import DelegateInferenceParams

    assert DelegateInferenceParams(
        prompt="hi", host_id="codex:host-adapter"
    ).host_id == "codex:host-adapter"
    # Preserve the established explicit-tool default; consult handles reciprocity.
    assert DelegateInferenceParams(prompt="hi").host_id == "claude:host-adapter"
    with pytest.raises(ValidationError):
        DelegateInferenceParams(prompt="hi", host_id="")


@pytest.mark.asyncio
async def test_delegate_inference_routes_to_codex(monkeypatch):
    """A codex call reaches the adapter and is reported as its own host.

    The exec JSONL compatibility path reports no model identifier, so its
    provenance carries a warning instead of implying a model id.
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
async def test_codex_jsonl_failure_reports_a_located_answer_region(monkeypatch):
    """The typed agent_message event is the primary answer boundary."""
    details = await _codex_failure_details(monkeypatch, _CODEX_JSONL_WITH_ANSWER)

    assert details["adapter_status"] == "malformed"
    assert details["adapter_answer_region_located"] is True
    assert "adapter_answer_region_note" not in details


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_delegate_inference_carries_model_reporting_status_and_reroutes(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _codex_host())
    monkeypatch.setattr(di, "get_context_resolved_agent_id", lambda: "uuid-requester")

    async def fake_invoke(host_id, prompt, **kwargs):
        return {
            "ok": True,
            "host_id": host_id,
            "text": "answer",
            "exit_status": 0,
            "provenance": {
                "model_used": None,
                "model_reporting_status": "unavailable_from_exec_jsonl",
                "model_reroutes": [
                    {"threadId": "t1", "turnId": "u1", "fromModel": "gpt-a", "toModel": "gpt-b", "reason": "capacity"},
                ],
                "tokens_used": 3,
                "terminal_answer": {
                    "schema": "unitares.terminal_answer.v1",
                    "status": "complete",
                },
            },
        }

    async def fake_track(*_args, **_kwargs):
        return None

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    monkeypatch.setattr(di, "_track_energy", fake_track)

    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "question",
        "host_id": "codex:host-adapter",
    }))

    inference = parsed["inference"]
    assert inference["model_used"] is None
    assert inference["model_reporting_status"] == "unavailable_from_exec_jsonl"
    assert inference["model_reroutes"][0]["toModel"] == "gpt-b"


@pytest.mark.asyncio
async def test_failed_delegation_names_its_privacy_class(monkeypatch):
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())

    async def fake_invoke(*_args, **_kwargs):
        return {
            "ok": False,
            "status": "still_running",
            "execution_id": "orch-1",
            "provenance": {},
        }

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hard problem",
        requesting_agent_uuid="uuid-requester",
        timeout_s=5,
    ))

    assert outcome.failure is not None
    details = outcome.failure.details
    assert details["host_id"] == "claude:host-adapter"
    assert details["provider_kind"] == "claude_host_adapter"
    assert details["privacy_class"] == "operator_authorized_external"



def _failed_adapter(error, *, phase="terminal"):
    async def fake_invoke(host_id, prompt, **kwargs):
        return {
            "ok": False, "host_id": host_id, "dispatch_phase": phase,
            "status": "malformed", "exit_status": 1, "execution_id": "ex-1",
            "error": error, "provenance": {},
        }
    return fake_invoke


@pytest.mark.asyncio
async def test_a_usage_limit_starts_a_cooldown_with_the_providers_reset(monkeypatch):
    from src.mcp_handlers.support import host_availability

    monkeypatch.setattr(di, "get_inference_host", lambda _h: _claude_host())
    monkeypatch.setattr(di, "invoke_host_adapter", _failed_adapter(
        "Claude CLI reported an error result: Claude AI usage limit reached|4102444800"))
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid=None))
    unavailable = outcome.failure.details["provider_unavailable"]
    assert unavailable["reason"] == "quota"
    assert unavailable["retry_after_source"] == "provider"
    assert outcome.failure.possibly_running is False
    assert host_availability.cooldown("claude:host-adapter") is not None

    # While cooling down, the next call is refused before any spawn and says when.
    monkeypatch.setattr(di, "invoke_host_adapter", _failed_adapter("must not run"))
    again = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid=None))
    assert again.failure.code == "INFERENCE_HOST_UNAVAILABLE"
    assert again.failure.execution_started is False
    assert "retry after" in again.failure.recovery["action"]


@pytest.mark.asyncio
async def test_our_own_spawn_rejection_is_not_a_provider_outage(monkeypatch):
    from src.mcp_handlers.support import host_availability

    monkeypatch.setattr(di, "get_inference_host", lambda _h: _claude_host())
    monkeypatch.setattr(di, "invoke_host_adapter", _failed_adapter(
        "spawn 401: unauthorized", phase="spawn_rejected"))
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid=None))
    assert "provider_unavailable" not in outcome.failure.details
    assert host_availability.cooldown("claude:host-adapter") is None


@pytest.mark.asyncio
async def test_an_unrecognised_failure_takes_no_host_out_of_rotation(monkeypatch):
    from src.mcp_handlers.support import host_availability

    monkeypatch.setattr(di, "get_inference_host", lambda _h: _claude_host())
    monkeypatch.setattr(di, "invoke_host_adapter", _failed_adapter(
        "Host CLI returned a malformed terminal answer envelope"))
    await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid=None))
    assert host_availability.cooldown("claude:host-adapter") is None


@pytest.mark.asyncio
async def test_a_success_clears_the_cooldown(monkeypatch):
    from src.mcp_handlers.support import host_availability

    host_availability.record_unavailable(
        "claude:host-adapter", {"reason": "quota", "stated_reset": None}, now=0.0)
    assert host_availability.cooldown("claude:host-adapter") is None  # lapsed
    assert host_availability._state["claude:host-adapter"]["failures"] == 1

    async def ok_invoke(host_id, prompt, **kwargs):
        return {"ok": True, "host_id": host_id, "text": "fine", "exit_status": 0,
                "execution_id": "ex", "provenance": {"terminal_answer": {
                    "schema": "unitares.terminal_answer.v1", "status": "complete"}}}

    async def no_track(*a, **k):
        return None

    monkeypatch.setattr(di, "get_inference_host", lambda _h: _claude_host())
    monkeypatch.setattr(di, "invoke_host_adapter", ok_invoke)
    monkeypatch.setattr(di, "_track_energy", no_track)
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid=None))
    assert outcome.ok
    assert "claude:host-adapter" not in host_availability._state


@pytest.mark.asyncio
async def test_a_host_switched_off_in_the_hosts_file_is_refused(monkeypatch, tmp_path):
    from src import inference_config

    p = tmp_path / "inference.yaml"
    p.write_text("version: 1\nhosts:\n  claude:host-adapter: { enabled: false }\n")
    p.chmod(0o600)
    monkeypatch.setenv(inference_config.HOSTS_FILE_ENV, str(p))
    inference_config._cache.update(key=None, result=None)
    monkeypatch.setattr(
        di, "get_inference_host", lambda _host_id: _claude_host(configured=True, available=True))
    parsed = _payload(await di.handle_delegate_inference({"prompt": "hello"}))
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert "inference hosts file" in parsed["error"] or "inference hosts file" in json.dumps(parsed)
    inference_config._cache.update(key=None, result=None)


# --- #2285: the default fits a ~60s per-tool MCP client timeout -------------


def test_default_timeout_answers_inside_a_60s_client_with_headroom():
    """The default await window leaves room for spawn and response under ~60s.

    A client that gives up first receives a bare transport error and never the
    execution id the recovery path depends on (#2285). Larger values remain
    accepted for clients that wait longer.
    """
    from src.mcp_handlers.schemas.core import (
        DELEGATE_INFERENCE_CLIENT_BUDGET_S,
        DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S,
        DELEGATE_INFERENCE_OVERHEAD_S,
        DELEGATE_INFERENCE_RESPONSE_HEADROOM_S,
        DelegateInferenceParams,
    )

    assert DELEGATE_INFERENCE_CLIENT_BUDGET_S == 60
    assert DELEGATE_INFERENCE_RESPONSE_HEADROOM_S > 0
    # The whole default call, overhead included, ends with headroom to spare.
    assert (
        DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S
        + DELEGATE_INFERENCE_OVERHEAD_S
        + DELEGATE_INFERENCE_RESPONSE_HEADROOM_S
        <= DELEGATE_INFERENCE_CLIENT_BUDGET_S
    )
    assert DelegateInferenceParams(prompt="x").timeout_s == (
        DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S
    )
    assert di.DelegatedInferenceRequest(
        prompt="x", requesting_agent_uuid=None
    ).timeout_s == DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S
    assert DelegateInferenceParams(prompt="x", timeout_s=420).timeout_s == 420


class _FakeOrchestratorResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_default_call_to_a_slow_child_returns_the_execution_id(monkeypatch):
    """Through the real adapter, a still-running child yields a structured
    timeout with the execution id inside the default window, and the child it
    leaves behind is bounded by the orchestrator backstop."""
    import httpx

    from src.mcp_handlers.support import host_adapter as ha

    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    posts = []

    class _Orchestrator:
        """Accepts the child, then reports it still running on every await."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            posts.append((url, kwargs.get("json")))
            if url.endswith("/v1/agents"):
                return _FakeOrchestratorResp(201, {"execution_id": "ex-slow"})
            return _FakeOrchestratorResp(504, {"error": "await timeout"})

    monkeypatch.setattr(httpx, "AsyncClient", _Orchestrator)

    parsed = _payload(await di.handle_delegate_inference({"prompt": "long review"}))

    assert parsed["success"] is False
    assert parsed["error_code"] == "DELEGATED_INFERENCE_TIMEOUT"
    assert parsed["orchestrator_execution_id"] == "ex-slow"
    assert parsed["dispatch_phase"] == "await_timeout"
    assert parsed["execution_started"] is True
    assert parsed["possibly_running"] is True
    assert parsed["timeout_s"] == 45
    assert parsed["orchestrator_backstop_s"] == 45 + ha.ORCHESTRATOR_BACKSTOP_GRACE_S
    action = parsed["recovery"]["action"]
    assert action.startswith("Do not retry yet.")
    assert "reconcile or terminate" in action
    assert f"{45 + ha.ORCHESTRATOR_BACKSTOP_GRACE_S}s" in action
    assert "per-tool timeout" in action

    spawn, awaited = posts
    assert spawn[1]["max_runtime_ms"] == (45 + ha.ORCHESTRATOR_BACKSTOP_GRACE_S) * 1000
    assert awaited[0].endswith("/v1/executions/ex-slow/await")
    assert awaited[1]["timeout_ms"] <= 45_000


@pytest.mark.asyncio
async def test_an_explicit_longer_timeout_is_still_honoured(monkeypatch):
    """Clients that wait longer (the SDK) keep the full window they ask for."""
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    captured = {}

    async def fake_invoke(host_id, prompt, **kwargs):
        captured.update(kwargs)
        return {
            "ok": False,
            "status": "still_running",
            "dispatch_phase": "await_timeout",
            "execution_id": "ex-long",
            "provenance": {},
        }

    monkeypatch.setattr(di, "invoke_host_adapter", fake_invoke)
    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "long review",
        "timeout_s": 300,
    }))

    assert captured["timeout_s"] == 300
    assert parsed["timeout_s"] == 300
    assert parsed["orchestrator_backstop_s"] == 330
    assert parsed["orchestrator_execution_id"] == "ex-long"


def _stalling_orchestrator(monkeypatch, *, stall_spawn=False):
    """Enable the real adapter against an orchestrator that hangs.

    It accepts the spawn and then never answers the await (or, with
    ``stall_spawn``, never answers the spawn). Neither the fake nor anything
    else enforces the per-request httpx timeout, so only the call's own
    deadline can end the wait.
    """
    import asyncio

    import httpx

    from src.mcp_handlers.support import host_adapter as ha

    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    monkeypatch.setattr(ha.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    posts = []

    class _Orchestrator:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            posts.append(url)
            if url.endswith("/v1/agents") and not stall_spawn:
                return _FakeOrchestratorResp(201, {"execution_id": "ex-stalled"})
            await asyncio.sleep(3600)
            raise AssertionError("the stall was not cut off")

    monkeypatch.setattr(httpx, "AsyncClient", _Orchestrator)
    return posts


# Scaled down so the test runs in about a second: the same arithmetic as the
# default (timeout_s + overhead, with the HTTP grace clipped to what is left).
_SCALED_TIMEOUT_S = 1
_SCALED_OVERHEAD_S = 0.5
_TIMING_SLACK_S = 0.5


@pytest.mark.asyncio
async def test_a_stalled_await_still_returns_the_execution_id_by_the_deadline(monkeypatch):
    """#2285 review: an orchestrator that accepts the child and then stalls
    the await must not hold the reply past timeout_s + overhead, or the
    client's own timeout wins and the execution id is lost."""
    import time

    posts = _stalling_orchestrator(monkeypatch)
    monkeypatch.setattr(di, "DELEGATE_INFERENCE_OVERHEAD_S", _SCALED_OVERHEAD_S)

    started = time.monotonic()
    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "long review",
        "timeout_s": _SCALED_TIMEOUT_S,
    }))
    elapsed = time.monotonic() - started

    assert elapsed <= _SCALED_TIMEOUT_S + _SCALED_OVERHEAD_S + _TIMING_SLACK_S
    assert parsed["success"] is False
    assert parsed["error_code"] == "DELEGATED_INFERENCE_TIMEOUT"
    assert parsed["orchestrator_execution_id"] == "ex-stalled"
    assert parsed["dispatch_phase"] == "await_timeout"
    assert parsed["possibly_running"] is True
    assert posts[-1].endswith("/v1/executions/ex-stalled/await")


@pytest.mark.asyncio
async def test_a_stalled_spawn_still_answers_by_the_deadline(monkeypatch):
    """A spawn that never answers cannot carry an id, but the caller still
    gets a structured possibly-running failure rather than a transport cut."""
    import time

    _stalling_orchestrator(monkeypatch, stall_spawn=True)
    monkeypatch.setattr(di, "DELEGATE_INFERENCE_OVERHEAD_S", _SCALED_OVERHEAD_S)

    started = time.monotonic()
    parsed = _payload(await di.handle_delegate_inference({
        "prompt": "long review",
        "timeout_s": _SCALED_TIMEOUT_S,
    }))
    elapsed = time.monotonic() - started

    assert elapsed <= _SCALED_TIMEOUT_S + _SCALED_OVERHEAD_S + _TIMING_SLACK_S
    assert parsed["success"] is False
    assert parsed["error_code"] == "DELEGATED_INFERENCE_FAILED"
    assert parsed["possibly_running"] is True
    assert "Do not retry yet." in parsed["recovery"]["action"]


@pytest.mark.asyncio
async def test_no_spawn_is_sent_once_the_deadline_has_passed(monkeypatch):
    """Preflight that used up the whole allowance proves no child exists."""
    from src.mcp_handlers.support import host_adapter as ha

    posts = _stalling_orchestrator(monkeypatch)
    result = await ha.invoke_host_adapter(
        "claude:host-adapter", "review", timeout_s=5, deadline=0.0,
    )

    assert posts == []
    assert result["ok"] is False
    assert result["dispatch_phase"] == "preflight"


async def _cancel_deferred_bookkeeping():
    import asyncio

    pending = list(di._background_tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    assert not di._background_tasks


@pytest.mark.asyncio
async def test_hung_accounting_after_a_late_answer_does_not_cost_the_answer(monkeypatch):
    """#2285 review: an answer that arrives near the deadline is returned by
    the deadline, with its execution id, even when Energy accounting hangs;
    the accounting is deferred to a tracked task and the response says so."""
    import asyncio
    import time

    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(di, "DELEGATE_INFERENCE_OVERHEAD_S", _SCALED_OVERHEAD_S)
    accounting_started = asyncio.Event()

    async def late_answer(host_id, prompt, **kwargs):
        # Answer with a sliver of the deadline left.
        await asyncio.sleep(max(0.0, kwargs["deadline"] - time.monotonic() - 0.1))
        return {
            "ok": True,
            "host_id": host_id,
            "text": "late but complete",
            "exit_status": 0,
            "execution_id": "ex-late",
            "provenance": {"terminal_answer": {
                "schema": "unitares.terminal_answer.v1", "status": "complete",
            }},
        }

    async def hung_accounting(*_args, **_kwargs):
        accounting_started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(di, "invoke_host_adapter", late_answer)
    monkeypatch.setattr(di, "_track_energy", hung_accounting)

    started = time.monotonic()
    try:
        parsed = _payload(await di.handle_delegate_inference({
            "prompt": "review",
            "timeout_s": _SCALED_TIMEOUT_S,
        }))
        elapsed = time.monotonic() - started

        assert elapsed <= _SCALED_TIMEOUT_S + _SCALED_OVERHEAD_S + _TIMING_SLACK_S
        assert parsed["success"] is True
        assert parsed["response"] == "late but complete"
        assert parsed["inference"]["orchestrator_execution_id"] == "ex-late"
        assert parsed["inference"]["accounting_deferred"] == ["energy"]
        # Still running, and held, not dropped.
        assert accounting_started.is_set()
        assert len(di._background_tasks) == 1
    finally:
        await _cancel_deferred_bookkeeping()


@pytest.mark.asyncio
async def test_prompt_accounting_is_not_reported_deferred(monkeypatch):
    async def ok_invoke(host_id, prompt, **kwargs):
        return {"ok": True, "host_id": host_id, "text": "fine", "exit_status": 0,
                "execution_id": "ex", "provenance": {"terminal_answer": {
                    "schema": "unitares.terminal_answer.v1", "status": "complete"}}}

    async def quick_track(*_args, **_kwargs):
        return None

    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(di, "invoke_host_adapter", ok_invoke)
    monkeypatch.setattr(di, "_track_energy", quick_track)
    outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
        prompt="hi", requesting_agent_uuid="requester"))

    assert outcome.ok
    assert "accounting_deferred" not in outcome.inference
    assert not di._background_tasks


@pytest.mark.asyncio
async def test_hung_cooldown_record_does_not_hold_a_failure_past_the_deadline(monkeypatch):
    """The failure path's provider-cooldown write is bounded the same way."""
    import asyncio
    import time

    from src.mcp_handlers.support import host_availability

    monkeypatch.setattr(di, "get_inference_host", lambda _host_id: _claude_host())
    monkeypatch.setattr(di, "DELEGATE_INFERENCE_OVERHEAD_S", _SCALED_OVERHEAD_S)
    monkeypatch.setattr(di, "invoke_host_adapter", _failed_adapter(
        "Claude CLI reported an error result: Claude AI usage limit reached|4102444800"))

    async def hung_record(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(host_availability, "record_unavailable_async", hung_record)

    started = time.monotonic()
    try:
        outcome = await di.run_delegated_inference(di.DelegatedInferenceRequest(
            prompt="hi", requesting_agent_uuid=None, timeout_s=_SCALED_TIMEOUT_S))
        elapsed = time.monotonic() - started

        assert elapsed <= _SCALED_TIMEOUT_S + _SCALED_OVERHEAD_S + _TIMING_SLACK_S
        assert outcome.failure.details["orchestrator_execution_id"] == "ex-1"
        assert outcome.failure.details["accounting_deferred"] == ["provider_cooldown"]
        assert "provider_unavailable" not in outcome.failure.details
    finally:
        await _cancel_deferred_bookkeeping()
