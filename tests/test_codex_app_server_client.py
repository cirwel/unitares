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
        "serviceName": "unitares_host_adapter",
    }
    # No "sandbox": it would select codex's legacy whole-disk-read sandbox over
    # the lane's deny-read permission profile.
    assert "sandbox" not in requests[2]["params"]


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


def test_lane_overrides_deny_reads_outside_runtime_and_codex_paths(tmp_path):
    """The legacy read-only sandbox reads the whole disk (a canary in the home
    directory was returned in an answer, 2026-10-15). Every lane selects a
    profile that denies reads except the runtime minimum and codex's own
    binary directories, and switches off tools a read-only lane never needs."""
    real_bin = tmp_path / "cask" / "1.0" / "bin"
    real_bin.mkdir(parents=True)
    real = real_bin / "codex"
    real.write_text("")
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    link = link_dir / "codex"
    link.symlink_to(real)

    overrides = client.lane_config_overrides(str(link))
    assert overrides[0] == f'default_permissions="{client.PERMISSION_PROFILE}"'
    table = overrides[1]
    assert table.startswith(f"permissions.{client.PERMISSION_PROFILE}.filesystem={{")
    assert '":root"="deny"' in table and '":minimal"="read"' in table
    assert f'"{link_dir}"="read"' in table
    assert f'"{real_bin.resolve()}"="read"' in table
    assert 'shell_environment_policy.inherit="core"' in overrides
    assert "mcp_servers={}" in overrides and "hooks={}" in overrides
    for feature in ("computer_use", "browser_use", "plugins", "apps"):
        assert f"features.{feature}=false" in overrides
    assert not any("sandbox_mode" in o for o in overrides)


def test_lane_override_count_is_the_same_for_every_cli(tmp_path):
    """Shell templates name each override slot, so the count must not vary."""
    plain = tmp_path / "codex"
    plain.write_text("")
    assert len(client.lane_config_overrides(str(plain))) == client.LANE_CONFIG_OVERRIDE_COUNT
    assert len(client.lane_config_overrides("codex")) == client.LANE_CONFIG_OVERRIDE_COUNT


def test_app_server_is_spawned_with_the_lane_overrides(monkeypatch):
    seen = {}

    class _Proc:
        stdin = stdout = None

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    client._spawn("/opt/x/codex")
    argv = seen["argv"]
    assert argv[:3] == ["/opt/x/codex", "app-server", "--stdio"]
    assert argv[3:] == client.lane_config_args("/opt/x/codex")
    assert "--sandbox" not in argv


def test_client_refuses_a_sandbox_other_than_read_only(monkeypatch):
    monkeypatch.setenv("HA_CLI", "/opt/x/codex")
    monkeypatch.setenv("HA_PROMPT", "hi")
    monkeypatch.setenv("HA_SANDBOX", "workspace-write")
    monkeypatch.setattr(client, "_spawn", lambda cli: (_ for _ in ()).throw(AssertionError("spawned")))
    assert client.main() == 1
