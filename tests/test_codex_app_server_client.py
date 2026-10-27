"""Tests for the one-shot Codex app-server protocol client."""

from __future__ import annotations

import io
import json
import os

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


def test_lane_overrides_deny_the_shared_temporary_directories():
    """``:minimal`` grants ``/tmp`` and ``/var/tmp``: a council codex lane
    returned a canary from ``/private/tmp`` in its answer (2026-10-26)."""
    table = client.lane_config_overrides("/opt/x/codex", "unitares_lane_test")[1]
    for root in client.TEMP_ROOTS:
        assert (f'"{root}"="deny"' in table) == os.path.isdir(root)
    assert '"/tmp"="deny"' in table
    # Denials come after every read entry, so none is mistaken for a grant.
    assert table.index('"/opt/x"="read"') < table.index('"/tmp"="deny"')


def test_a_path_given_under_a_temporary_root_does_not_lift_its_denial(tmp_path, monkeypatch):
    """A deny outranks every read in codex's profile, so a workspace under
    ``/tmp`` cannot be granted without granting all of ``/tmp``. The root stays
    denied (Codex review of #3103: an exception reopened the whole root)."""
    shared = tmp_path / "private" / "tmp"
    (shared / "repo").mkdir(parents=True)
    monkeypatch.setattr(client, "TEMP_ROOTS", (str(shared), str(tmp_path / "absent")))

    table = client.lane_config_overrides(
        "/opt/x/codex", "unitares_lane_test", extra_read=[str(shared / "repo")]
    )[1]
    assert f'"{shared}"="deny"' in table
    # A root that does not exist here (``/private/...`` off macOS) is left out.
    assert "absent" not in table


def test_under_temp_root_resolves_links_and_respects_name_boundaries(tmp_path, monkeypatch):
    shared = tmp_path / "private" / "tmp"
    (shared / "repo").mkdir(parents=True)
    link = tmp_path / "tmp"
    link.symlink_to(shared)
    monkeypatch.setattr(client, "TEMP_ROOTS", (str(link),))

    assert client.under_temp_root(str(link / "repo"))
    # Through the link's target, as macOS's /private/tmp is /tmp's.
    assert client.under_temp_root(str(shared / "repo"))
    assert client.under_temp_root(str(shared))
    # A sibling whose name merely begins like the root is not under it.
    assert not client.under_temp_root(str(shared) + "-other")


def test_temp_denials_keep_the_override_count(tmp_path):
    """The denials live inside the filesystem table, not in new overrides."""
    assert (
        len(client.lane_config_overrides("codex", extra_read=[str(tmp_path)]))
        == client.LANE_CONFIG_OVERRIDE_COUNT
    )


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
    monkeypatch.setattr(client, "confinement_verified", lambda cli: True)
    monkeypatch.setattr(client, "admin_config_conflict", lambda paths=None: None)
    listed = []

    def stubborn(cli, overrides, cwd):
        listed.append(cwd)
        return ["stubborn"]

    monkeypatch.setattr(client, "_enabled_mcp_servers", stubborn)
    assert client.main() == 1  # not FALLBACK_SAFE_EXIT
    assert listed  # the refusal came from the MCP check, not an earlier one


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
    # Refused by the admin check itself: neither the probe nor a spawn runs.
    monkeypatch.setattr(client, "confinement_verified",
                        lambda cli: (_ for _ in ()).throw(AssertionError("probed first")))
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


def test_the_per_user_mdm_path_survives_an_unset_user(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setattr(client.getpass, "getuser", lambda: "svcuser")
    assert "/Library/Managed Preferences/svcuser/com.openai.codex.plist" in client.admin_config_paths()


def test_a_stalled_mcp_listing_refuses_rather_than_falls_back(monkeypatch):
    def stall(*a, **k):
        raise client.subprocess.TimeoutExpired("codex", 30)

    monkeypatch.setattr(client.subprocess, "run", stall)
    with pytest.raises(ValueError, match="could not run"):
        client._enabled_mcp_servers("/opt/x/codex", [], "/w")


def test_a_failed_probe_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_CONFINEMENT_VERIFIED", {})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli = _fake_codex(tmp_path, 'echo "unknown command" >&2; exit 2\n')
    assert client.confinement_verified(cli) is False
    assert client._CONFINEMENT_VERIFIED == {}


def test_the_probe_runs_in_an_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_CONFINEMENT_VERIFIED", {})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen = tmp_path / "cwd.txt"
    cli = _fake_codex(tmp_path, f'pwd >> "{seen}"; ls -A >> "{seen}"; '
                                'while [ "$1" != "--" ]; do shift; done; shift\n'
                                '[ "$1" = "/usr/bin/true" ] && exit 0\nexit 1\n')
    monkeypatch.chdir(tmp_path)
    assert client.confinement_verified(cli) is True
    lines = seen.read_text().splitlines()
    assert lines and all(l.endswith("/work") for l in lines if l.startswith("/"))
    assert not [l for l in lines if not l.startswith("/")]  # nothing listed: empty


# --- the host.codex launch profile's in-client exec fallback ----------------

def _profile_env(monkeypatch, **extra):
    monkeypatch.setenv("HA_CLI", "/opt/x/codex")
    monkeypatch.setenv("HA_PROMPT", "--version")
    monkeypatch.setenv(client.EXEC_FALLBACK_ENV, "1")
    monkeypatch.delenv("HA_MODEL", raising=False)
    monkeypatch.delenv("HA_CODEX_APP_SERVER", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(client, "confinement_verified", lambda cli: True)
    monkeypatch.setattr(client, "admin_config_conflict", lambda paths=None: None)


def _record_exec(monkeypatch, returncode=0):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return client.subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    return calls


def test_exec_command_reads_the_prompt_from_stdin():
    args = client.exec_command("/opt/x/codex", "gpt-5.5")
    assert args[:5] == ["/opt/x/codex", "exec", "--ignore-user-config", "--ephemeral", "--json"]
    assert args[-3:] == ["-m", "gpt-5.5", "-"]
    assert args.count("-c") == client.LANE_CONFIG_OVERRIDE_COUNT


def test_profile_mode_retries_a_fallback_safe_failure_through_exec(monkeypatch):
    _profile_env(monkeypatch)
    monkeypatch.setattr(client, "app_server_main", lambda: client.FALLBACK_SAFE_EXIT)
    calls = _record_exec(monkeypatch, returncode=0)
    assert client.main() == 0
    (args, kwargs), = calls
    assert kwargs["input"] == "--version"
    assert "--version" not in args
    assert "unitares-codex-lane-" in kwargs["cwd"]


def test_profile_mode_never_retries_after_a_turn_was_sent(monkeypatch):
    _profile_env(monkeypatch)
    monkeypatch.setattr(client, "app_server_main", lambda: 1)
    calls = _record_exec(monkeypatch)
    assert client.main() == 1
    assert calls == []


def test_app_server_off_goes_straight_to_exec(monkeypatch):
    _profile_env(monkeypatch, HA_CODEX_APP_SERVER="0")
    monkeypatch.setattr(client, "app_server_main",
                        lambda: (_ for _ in ()).throw(AssertionError("app-server ran")))
    calls = _record_exec(monkeypatch, returncode=3)
    assert client.main() == 3
    assert len(calls) == 1


def test_without_the_profile_switch_exit_75_is_left_to_the_shell(monkeypatch):
    _profile_env(monkeypatch)
    monkeypatch.delenv(client.EXEC_FALLBACK_ENV)
    monkeypatch.setattr(client, "app_server_main", lambda: client.FALLBACK_SAFE_EXIT)
    calls = _record_exec(monkeypatch)
    assert client.main() == client.FALLBACK_SAFE_EXIT
    assert calls == []


@pytest.mark.parametrize("refusal", ["admin", "probe", "sandbox"])
def test_the_exec_fallback_runs_the_lane_checks_itself(monkeypatch, refusal):
    _profile_env(monkeypatch, HA_CODEX_APP_SERVER="0")
    if refusal == "admin":
        monkeypatch.setattr(client, "admin_config_conflict", lambda paths=None: "/etc/codex/config.toml")
    elif refusal == "probe":
        monkeypatch.setattr(client, "confinement_verified", lambda cli: False)
    else:
        monkeypatch.setenv("HA_SANDBOX", "workspace-write")
    calls = _record_exec(monkeypatch)
    assert client.main() == 1
    assert calls == []
