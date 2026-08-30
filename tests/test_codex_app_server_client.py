"""Tests for the one-shot Codex app-server protocol client."""

from __future__ import annotations

import io
import json

import pytest

from src.mcp_handlers.support import codex_app_server_client as client


def _lines(*messages):
    return io.StringIO("".join(json.dumps(message) + "\n" for message in messages))


def _initialize_response():
    return {
        "id": 0,
        "result": {
            "userAgent": "unitares_host_adapter/0.151.0 (Mac OS; arm64)",
        },
    }


def _thread_response():
    return {
        "id": 1,
        "result": {
            "thread": {"id": "thread-1"},
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
            "serviceTier": "priority",
        },
    }


def test_protocol_reports_effective_model_reroute_and_usage():
    terminal = {
        "schema": "unitares.terminal_answer.v1",
        "status": "complete",
        "answer": "instrumented",
    }
    stdout = _lines(
        _initialize_response(),
        {"method": "thread/started", "params": {"thread": {"id": "thread-1"}}},
        _thread_response(),
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        },
        {
            "id": 2,
            "result": {
                "turn": {"id": "turn-1", "status": "inProgress", "items": []}
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": json.dumps(terminal),
                },
            },
        },
        {
            "method": "model/rerouted",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "fromModel": "gpt-5.6-sol",
                "toModel": "gpt-5.6-terra",
                "reason": "highRiskCyberActivity",
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "cacheWriteInputTokens": 3,
                        "outputTokens": 5,
                        "reasoningOutputTokens": 4,
                        "totalTokens": 15,
                    },
                    "total": {},
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "items": []},
            },
        },
    )
    stdin = io.StringIO()

    result = client.run_protocol(
        stdin,
        stdout,
        prompt="answer exactly",
        model=None,
        sandbox="read-only",
        cwd="/repo",
    )

    assert json.loads(result["text"]) == terminal
    assert result["model_selected"] == "gpt-5.6-sol"
    assert result["model_effective"] == "gpt-5.6-terra"
    assert result["model_used"] == "gpt-5.6-terra"
    assert result["models_used"] == ["gpt-5.6-terra"]
    assert result["model_provider"] == "openai"
    assert result["service_tier"] == "priority"
    assert result["model_reroutes"][0]["fromModel"] == "gpt-5.6-sol"
    assert result["provider_usage"] == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_output_tokens": 4,
        "total_tokens": 15,
    }
    assert result["tokens_used"] == 15
    assert result["codex_cli_version"] == "0.151.0"
    requests = [json.loads(line) for line in stdin.getvalue().splitlines()]
    assert requests[2]["params"] == {
        "approvalPolicy": "never",
        "cwd": "/repo",
        "ephemeral": True,
        "sandbox": "read-only",
        "serviceName": "unitares_host_adapter",
    }


def test_protocol_failure_before_turn_is_fallback_safe():
    stdout = _lines(
        _initialize_response(),
        {"id": 1, "error": {"code": -32601, "message": "unsupported"}},
    )

    with pytest.raises(client.AppServerClientError) as exc_info:
        client.run_protocol(
            io.StringIO(),
            stdout,
            prompt="answer",
            model=None,
            sandbox="read-only",
            cwd="/repo",
        )

    assert exc_info.value.fallback_safe is True


def test_protocol_failure_after_turn_request_is_not_retried():
    stdout = _lines(_initialize_response(), _thread_response())

    with pytest.raises(client.AppServerClientError) as exc_info:
        client.run_protocol(
            io.StringIO(),
            stdout,
            prompt="answer",
            model="gpt-5.6-sol",
            sandbox="read-only",
            cwd="/repo",
        )

    assert exc_info.value.fallback_safe is False
