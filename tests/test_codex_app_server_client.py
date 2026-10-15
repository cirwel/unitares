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

    overrides = client.lane_config_overrides(str(link), "unitares_lane_test")
    assert overrides[0] == 'default_permissions="unitares_lane_test"'
    table = overrides[1]
    assert table.startswith("permissions.unitares_lane_test.filesystem={")
    assert '":root"="deny"' in table and '":minimal"="read"' in table
    assert f'"{link_dir}"="read"' in table
    assert f'"{real_bin.resolve()}"="read"' in table
    policy = [o for o in overrides if o.startswith("shell_environment_policy")]
    # -c deep-merges tables, so an operator's ``set`` survives any table given
    # here; the ``include_only`` list (lists replace, and it applies after
    # ``set``) is what keeps injected variables away from the agent's commands.
    assert policy[0] == 'shell_environment_policy.inherit="core"'
    allowed = json.loads(policy[1].split("=", 1)[1])
    assert "HOME" in allowed and "PATH" in allowed
    assert not any("KEY" in n or "TOKEN" in n or "SECRET" in n or n == "*" for n in allowed)
    for feature in ("computer_use", "browser_use", "plugins", "apps", "hooks"):
        assert f"features.{feature}=false" in overrides
    assert not any("sandbox_mode" in o or o.endswith("={}") for o in overrides)


def test_each_launch_takes_a_fresh_profile_name():
    """A fixed name could be pre-populated by the operator's config with grants
    of its own, which -c's deep merge would keep alongside the denials."""
    first = client.lane_config_overrides("/opt/x/codex")[0]
    second = client.lane_config_overrides("/opt/x/codex")[0]
    assert first.startswith(f'default_permissions="{client.PERMISSION_PROFILE_PREFIX}')
    assert first != second


def test_lane_override_count_is_the_same_for_every_cli(tmp_path):
    """Shell templates name each override slot, so the count must not vary."""
    plain = tmp_path / "codex"
    plain.write_text("")
    assert len(client.lane_config_overrides(str(plain))) == client.LANE_CONFIG_OVERRIDE_COUNT
    assert len(client.lane_config_overrides("codex")) == client.LANE_CONFIG_OVERRIDE_COUNT


def test_every_enabled_mcp_server_is_switched_off_by_name(monkeypatch):
    """``-c`` deep-merges tables, so ``mcp_servers={}`` leaves the operator's
    servers running (verified with ``codex mcp list``); each server the loaded
    configuration still enables is disabled by name and the result re-checked."""
    calls = []

    def fake_enabled(cli, overrides, cwd):
        calls.append(list(overrides))
        return [] if any(o.endswith(".enabled=false") for o in overrides) else ["node_repl", "computer-use"]

    monkeypatch.setattr(client, "_enabled_mcp_servers", fake_enabled)
    assert client.mcp_disable_overrides("/opt/x/codex", ["base"], "/w") == [
        "mcp_servers.node_repl.enabled=false",
        "mcp_servers.computer-use.enabled=false",
    ]
    assert len(calls) == 2


def test_a_server_that_stays_enabled_or_cannot_be_named_fails_closed(monkeypatch):
    monkeypatch.setattr(client, "_enabled_mcp_servers", lambda cli, overrides, cwd: ["stubborn"])
    with pytest.raises(ValueError, match="still enabled"):
        client.mcp_disable_overrides("/opt/x/codex", [], "/w")
    monkeypatch.setattr(client, "_enabled_mcp_servers", lambda cli, overrides, cwd: ["has.dot"])
    with pytest.raises(ValueError, match="cannot disable"):
        client.mcp_disable_overrides("/opt/x/codex", [], "/w")


def test_app_server_runs_in_the_workdir_with_the_lane_arguments(monkeypatch, tmp_path):
    seen = {}

    class _Proc:
        stdin = stdout = None

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(client, "_enabled_mcp_servers", lambda cli, overrides, cwd: [] if any(
        o.endswith(".enabled=false") for o in overrides) else ["helper"])
    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    client._spawn("/opt/x/codex", str(tmp_path))
    argv = seen["argv"]
    assert argv[:3] == ["/opt/x/codex", "app-server", "--stdio"]
    assert "mcp_servers.helper.enabled=false" in argv
    assert any(a.startswith('default_permissions="unitares_lane_') for a in argv)
    assert "--sandbox" not in argv
    assert seen["cwd"] == str(tmp_path)


def test_lane_workdir_is_empty_and_outside_the_repository():
    workdir = client.lane_workdir()
    try:
        import os
        assert os.listdir(workdir) == []
        assert not os.path.abspath(workdir).startswith(os.path.abspath("."))
    finally:
        import shutil
        shutil.rmtree(workdir)


def test_client_refuses_a_sandbox_other_than_read_only(monkeypatch):
    monkeypatch.setenv("HA_CLI", "/opt/x/codex")
    monkeypatch.setenv("HA_PROMPT", "hi")
    monkeypatch.setenv("HA_SANDBOX", "workspace-write")
    monkeypatch.setattr(client, "_spawn", lambda cli, cwd: (_ for _ in ()).throw(AssertionError("spawned")))
    assert client.main() == 1


def test_an_unneutralisable_config_refuses_without_the_exec_fallback(monkeypatch):
    monkeypatch.setenv("HA_CLI", "/opt/x/codex")
    monkeypatch.setenv("HA_PROMPT", "hi")
    monkeypatch.setattr(client, "_enabled_mcp_servers", lambda cli, overrides, cwd: ["stubborn"])
    assert client.main() == 1  # not FALLBACK_SAFE_EXIT


def test_an_npm_codex_launcher_grants_the_openai_scope(tmp_path):
    """npm resolves codex to a JS launcher; the native binary the sandbox
    re-executes lives in the sibling @openai/codex-<platform> package."""
    scope = tmp_path / "lib" / "node_modules" / "@openai"
    launcher = scope / "codex" / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "codex").symlink_to(launcher)
    assert str(scope.resolve()) in client.codex_readable_roots(str(link_dir / "codex"))


def test_any_administrator_codex_config_refuses_the_lane(tmp_path, monkeypatch):
    """Managed layers outrank -c overrides and a quoted "sandbox_mode" key is
    valid TOML, so existence alone refuses rather than a contents check."""
    admin = tmp_path / "managed_config.toml"
    admin.write_text('"sandbox_mode" = "danger-full-access"\n')
    assert client.admin_config_conflict([str(tmp_path / "missing")]) is None
    assert client.admin_config_conflict([str(admin)]) == str(admin)
    assert "/etc/codex/managed_config.toml" in client.admin_config_paths()
    assert "/etc/codex/requirements.toml" in client.admin_config_paths()

    monkeypatch.setenv("HA_CLI", "/opt/x/codex")
    monkeypatch.setenv("HA_PROMPT", "hi")
    monkeypatch.setattr(client, "admin_config_conflict", lambda paths=None: str(admin))
    monkeypatch.setattr(client, "_spawn", lambda cli, cwd: (_ for _ in ()).throw(AssertionError("spawned")))
    assert client.main() == 1


def _fake_codex(tmp_path, body):
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\n" + body)
    cli.chmod(0o755)
    return str(cli)


def test_a_codex_that_ignores_the_profile_is_not_verified(tmp_path, monkeypatch):
    """An older codex can accept the overrides and confine nothing; the probe
    sees it read the file and refuses."""
    monkeypatch.setattr(client, "_CONFINEMENT_VERIFIED", {})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # "sandbox": skip arguments up to "--", then run the command unconfined.
    cli = _fake_codex(tmp_path, 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n')
    assert client.confinement_verified(cli) is False


def test_a_codex_without_a_sandbox_command_is_not_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_CONFINEMENT_VERIFIED", {})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli = _fake_codex(tmp_path, 'echo "unknown command" >&2; exit 2\n')
    assert client.confinement_verified(cli) is False


def test_a_confining_codex_is_verified_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_CONFINEMENT_VERIFIED", {})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Runs `true`, refuses to read anything: what enforcement looks like.
    cli = _fake_codex(tmp_path, 'while [ "$1" != "--" ]; do shift; done; shift\n'
                                '[ "$1" = "/usr/bin/true" ] && exit 0\n'
                                'echo "Operation not permitted" >&2; exit 1\n')
    assert client.confinement_verified(cli) is True
    assert len(client._CONFINEMENT_VERIFIED) == 1
