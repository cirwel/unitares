"""scripts/ops/orchestrator-profiles.py, the launch profiles generator.

The Elixir suite loads ``elixir/agent_orchestrator/test/fixtures/generated_profiles.json``
and runs it (generated_profiles_test.exs); the first test here keeps that
fixture equal to what the generator writes, so the two cannot drift apart.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "orchestrator-profiles.py"
FIXTURE = REPO_ROOT / "elixir" / "agent_orchestrator" / "test" / "fixtures" / "generated_profiles.json"

_spec = importlib.util.spec_from_file_location("orchestrator_profiles", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def placeholder_document():
    """The fixture: the generator's output for placeholder paths and a server
    with an MCP bearer and no other reviewer settings."""
    from src import dialectic_reviewer_config as rc

    settings = {rc.MCP_BEARER_ENV: "1"}
    fixed, secrets = rc.reviewer_profile_env(settings)
    clis = {"UNITARES_CLAUDE_CLI": "/usr/local/bin/claude",
            "UNITARES_CODEX_CLI": "/usr/local/bin/codex",
            "UNITARES_ANTIGRAVITY_CLI": "/usr/local/bin/agy"}
    return gen.build_profiles(
        repo_root="/srv/unitares", python="/usr/bin/python3", user="operator",
        claude_cli="/usr/local/bin/claude", codex_cli="/usr/local/bin/codex",
        antigravity_cli="/usr/local/bin/agy", council_roots=["/srv/repos"],
        reviewer={"fixed": fixed, "secrets": secrets, "clis": clis,
                  "max_runtime_ms": rc.reviewer_max_runtime_ms(settings)})


def test_fixture_is_the_generator_output_for_placeholder_paths():
    assert FIXTURE.read_text(encoding="utf-8") == gen.render(placeholder_document())


def test_model_pattern_matches_the_client():
    from src.mcp_handlers.support import claude_cli_client
    assert gen.MODEL_PATTERN == claude_cli_client.MODEL_PATTERN.pattern


def test_no_cli_means_no_profile():
    assert gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli=None)[
        "profiles"] == {}
    only_codex = gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli=None,
                                    codex_cli="/c")["profiles"]
    assert list(only_codex) == ["host.codex"]


def test_every_client_the_profiles_name_exists_in_this_checkout():
    document = gen.build_profiles(repo_root=str(REPO_ROOT), python=sys.executable, user="u",
                                  claude_cli="/a", codex_cli="/b", antigravity_cli="/c")
    for profile in document["profiles"].values():
        assert profile["args"][0] == "-I"
        assert Path(profile["args"][1]).is_file()


def test_agy_prompt_cap_matches_its_client():
    from src.mcp_handlers.support import antigravity_cli_client as agy
    overhead = len(agy.guard_prompt("").encode()) + len(agy.TEXT_ONLY.encode())
    assert gen.AGY_PROMPT_MAX_BYTES == agy.PROMPT_BYTES_LIMIT - overhead


def test_every_prompt_cap_fits_linux_env_string_limit():
    document = gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli="/a",
                                  codex_cli="/b", antigravity_cli="/c")
    for profile in document["profiles"].values():
        name = "HA_PROMPT" if "HA_PROMPT" in profile["vars"] else "COUNCIL_PROMPT"
        assert len(name) + 1 + profile["vars"][name]["max_bytes"] + 1 <= 131_072


def test_codex_profile_runs_the_exec_fallback_in_client():
    profile = gen.codex_profile(repo_root="/r", python="/p", cli="/c", user="u",
                                app_server=True)
    fixed = profile["env"]["set"]
    assert fixed["HA_CODEX_EXEC_FALLBACK"] == "1"
    assert fixed["HA_SANDBOX"] == "read-only"
    assert fixed["HA_CODEX_APP_SERVER"] == "1"


@pytest.mark.parametrize("value, expected", [(None, True), ("1", True), ("off", False),
                                             ("0", False), ("False", False)])
def test_app_server_switch_reads_like_the_host_adapter(value, expected):
    assert gen.flag_on(value) is expected


def test_no_host_profile_binds_a_secret_or_inherits_an_api_key():
    document = gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli="/a",
                                  codex_cli="/b", antigravity_cli="/c")
    assert all(name.startswith(("host.", "council.")) for name in document["profiles"])
    for profile in document["profiles"].values():
        assert "secrets" not in profile["env"]
        assert not any("KEY" in name or "TOKEN" in name for name in profile["env"]["inherit"])


def test_env_file_override_pins_the_symlink_not_its_target(tmp_path):
    target = tmp_path / "versions" / "claude-2.0"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    target.chmod(0o700)
    link = tmp_path / "claude"
    link.symlink_to(target)
    env_file = tmp_path / "server.env"
    env_file.write_text(f"# comment\nexport UNITARES_CLAUDE_CLI=\"{link}\"\nOTHER=1\n")
    settings = gen.read_env_files([str(env_file)])
    assert gen.find_cli("claude", settings["UNITARES_CLAUDE_CLI"], "/nohome") == str(link)


def test_a_relative_override_is_refused():
    with pytest.raises(SystemExit, match="absolute"):
        gen.find_cli("claude", "bin/claude", "/nohome")


def test_later_env_files_win(tmp_path):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("UNITARES_CLAUDE_CLI=/a\n")
    second.write_text("UNITARES_CLAUDE_CLI='/b'\n")
    assert gen.read_env_files([str(first), str(second)])["UNITARES_CLAUDE_CLI"] == "/b"


def test_a_missing_env_file_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit, match="cannot read env file"):
        gen.read_env_files([str(tmp_path / "absent.env")])


def _main(tmp_path, *extra):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)
    env_file = tmp_path / "server.env"
    env_file.write_text(f"UNITARES_CLAUDE_CLI={cli}\n")
    out = tmp_path / "state" / "profiles.json"
    code = gen.main(["--out", str(out), "--repo-root", str(REPO_ROOT),
                     "--python", sys.executable, "--env-file", str(env_file), *extra])
    return code, out, cli


def test_writes_a_private_file_and_leaves_it_alone_when_unchanged(tmp_path, capsys):
    code, out, cli = _main(tmp_path)
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    profile = json.loads(out.read_text())["profiles"]["host.claude"]
    assert profile["env"]["set"]["HA_CLI"] == str(cli)
    assert profile["cmd"] == os.path.abspath(sys.executable)
    assert "written" in capsys.readouterr().out

    before = out.stat().st_mtime_ns
    assert _main(tmp_path)[0] == 0
    assert out.stat().st_mtime_ns == before
    assert "unchanged" in capsys.readouterr().out


def test_check_reports_stale_without_writing(tmp_path):
    code, out, _ = _main(tmp_path, "--check")
    assert code == 1
    assert not out.exists()
    _main(tmp_path)
    assert _main(tmp_path, "--check")[0] == 0
    out.write_text("{}\n")
    assert _main(tmp_path, "--check")[0] == 1
    assert out.read_text() == "{}\n"


def test_refuses_a_directory_others_can_write(tmp_path):
    shared = tmp_path / "state"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(SystemExit, match="writable by group or others"):
        _main(tmp_path)


def test_refuses_a_root_that_is_not_a_checkout(tmp_path):
    with pytest.raises(SystemExit, match="not a UNITARES checkout"):
        gen.main(["--out", str(tmp_path / "p.json"), "--repo-root", str(tmp_path)])


def test_never_reads_its_own_environment(tmp_path, monkeypatch):
    # start.sh sources the secrets file before running the generator.
    monkeypatch.setenv("UNITARES_CLAUDE_CLI", "/from/the/environment")
    code, out, cli = _main(tmp_path)
    assert code == 0
    assert "/from/the/environment" not in out.read_text()


def test_right_text_with_a_loose_mode_is_rewritten_private(tmp_path, capsys):
    _, out, _ = _main(tmp_path)
    out.chmod(0o666)
    assert _main(tmp_path)[0] == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "written" in capsys.readouterr().out.splitlines()[-1]


def test_a_symlink_in_place_of_the_file_is_replaced(tmp_path):
    _, out, _ = _main(tmp_path)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(out.read_text())
    out.unlink()
    out.symlink_to(elsewhere)
    assert _main(tmp_path)[0] == 0
    assert not out.is_symlink()



# --- the dialectic.reviewer profile ------------------------------------------

from src import dialectic_reviewer_config as rc  # noqa: E402


def test_settings_keep_only_used_names_and_credentials_only_as_presence():
    sources = [{"UNITARES_DIALECTIC_REVIEWER_HOST": "codex", "AGENT_ORCHESTRATOR_BEARER_TOKEN": "t0ken-abc",
                rc.MCP_BEARER_ENV: "s3cret-bearer", "UNRELATED": "x"},
               {"UNITARES_DIALECTIC_REVIEWER_HOST": "claude"}]
    settings = gen.server_settings(sources, rc.REVIEWER_PROFILE_SETTINGS, (rc.MCP_BEARER_ENV,))
    assert settings == {"UNITARES_DIALECTIC_REVIEWER_HOST": "claude", rc.MCP_BEARER_ENV: "1"}


def test_reviewer_profile_env_binds_credentials_by_name_only():
    fixed, secrets = rc.reviewer_profile_env({
        rc.MCP_BEARER_ENV: "1",
        "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV": "EXAMPLE_API_KEY",
        "UNITARES_DIALECTIC_CODEX_TIMEOUT_S": "inf",
        "UNITARES_GOVERNANCE_URL": "http://127.0.0.1:8767",
    })
    assert secrets == {rc.MCP_BEARER_ENV: rc.MCP_BEARER_ENV, "EXAMPLE_API_KEY": "EXAMPLE_API_KEY"}
    assert rc.MCP_BEARER_ENV not in fixed
    assert fixed["UNITARES_DIALECTIC_CODEX_TIMEOUT_S"] == "420"  # unusable -> default
    assert fixed["UNITARES_GOVERNANCE_URL"] == "http://127.0.0.1:8767/mcp/"


def test_no_external_key_is_bound_without_an_external_host():
    _, secrets = rc.reviewer_profile_env({"UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV": "EXAMPLE_API_KEY"})
    assert secrets == {}


@pytest.mark.parametrize("name", ["not a name", "UNITARES_DIALECTIC_REVIEWER_HOST"])
def test_a_bad_external_key_name_is_refused(name):
    with pytest.raises(ValueError, match="must name an environment variable"):
        rc.reviewer_profile_env({"UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://e.invalid/v1",
                                 "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV": name,
                                 "UNITARES_DIALECTIC_REVIEWER_HOST": "codex"})


def test_reviewer_profile_takes_only_a_session_id():
    profile = placeholder_document()["profiles"]["dialectic.reviewer"]
    assert list(profile["vars"]) == ["DIALECTIC_SESSION_ID"]
    assert profile["vars"]["DIALECTIC_SESSION_ID"]["required"] is True
    fixed = profile["env"]["set"]
    assert fixed["DIALECTIC_REVIEWER_FROM_SESSION"] == "1"
    assert "UNITARES_DIALECTIC_REVIEWER_HOSTS_ERROR" in fixed
    assert not any(name.startswith("DIALECTIC_THESIS") for name in fixed)
    assert profile["lineage"] == "none"
    assert "lease" not in profile  # best-effort presence, as the raw spawn
    assert profile["args"] == ["-s", "-m", "agents.dialectic_reviewer"]


def test_reviewer_lifetime_matches_the_server(monkeypatch):
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    for wait in (None, "120", "-5", "inf"):
        if wait is None:
            monkeypatch.delenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", raising=False)
        else:
            monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", wait)
        monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOSTS", "codex,external")
        monkeypatch.delenv("UNITARES_INFERENCE_HOSTS_FILE", raising=False)
        settings = {k: v for k, v in os.environ.items() if k in rc.REVIEWER_PROFILE_SETTINGS}
        assert rc.reviewer_max_runtime_ms(settings) == od._reviewer_max_runtime_ms()


_JOB_FILE = ("version: 1\nhosts:\n  claude:host-adapter: { enabled: false }\n"
             "jobs:\n  dialectic_reviewer: { prefer: [claude:host-adapter, "
             "antigravity:host-adapter, codex:host-adapter] }\n")


@pytest.mark.parametrize("hosts, host, hosts_file", [
    ("codex,antigravity", "", None), ("", "claude", None), ("", "", None),
    ("claude", "", _JOB_FILE),                       # the job wins over the setting
    ("claude", "", "version: 1\nhosts: {bad\n"),   # a file that does not load
])
def test_the_profile_fixes_the_hosts_the_server_would_send(monkeypatch, tmp_path, hosts, host,
                                                           hosts_file):
    from src import inference_config
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    if hosts_file is None:
        monkeypatch.delenv(inference_config.HOSTS_FILE_ENV, raising=False)
    else:
        path = tmp_path / "inference.yaml"
        path.write_text(hosts_file)
        path.chmod(0o600)
        monkeypatch.setenv(inference_config.HOSTS_FILE_ENV, str(path))
    inference_config._cache.update(key=None, result=None)
    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOSTS", hosts)
    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOST", host)
    keys = ("UNITARES_DIALECTIC_REVIEWER_HOSTS", "UNITARES_DIALECTIC_REVIEWER_HOST",
            rc.REVIEWER_HOSTS_ERROR_ENV)
    sent = {k: od._build_spec("s", {"root_cause": "r"}, None)["env"][k] for k in keys}

    fixed, _ = rc.reviewer_profile_env(dict(os.environ))
    assert {k: fixed[k] for k in keys} == sent
    inference_config._cache.update(key=None, result=None)


def test_the_profile_starts_the_reviewer_in_an_empty_environment(tmp_path):
    """cmd, args, cd and PYTHONPATH import the reviewer with nothing inherited
    but the profile's list. Governance is unreachable here, so it must reach
    its session read and fail that loudly (exit 1), not fail to import."""
    import subprocess

    fixed, _ = rc.reviewer_profile_env({"UNITARES_GOVERNANCE_URL": "http://127.0.0.1:9/mcp/"})
    profile = gen.reviewer_profile(repo_root=str(REPO_ROOT), python=sys.executable, user="u",
                                   fixed=fixed, secrets={}, max_runtime_ms=1, clis={})
    env = {k: os.environ[k] for k in profile["env"]["inherit"] if k in os.environ}
    env.update(profile["env"]["set"], DIALECTIC_SESSION_ID="0123456789abcdef")
    proc = subprocess.run([profile["cmd"], *profile["args"]], cwd=profile["cd"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not read session 0123456789abcdef" in proc.stdout



def test_the_default_external_key_variable_is_the_reviewers():
    from agents.dialectic_reviewer import host_backends
    assert rc.DEFAULT_EXTERNAL_KEY_ENV == host_backends._DEFAULT_KEY_ENV


def test_the_default_external_key_is_bound_when_the_server_has_it():
    external = {"UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://e.invalid/v1"}
    _, secrets = rc.reviewer_profile_env({**external, rc.DEFAULT_EXTERNAL_KEY_ENV: "1"})
    assert secrets == {rc.DEFAULT_EXTERNAL_KEY_ENV: rc.DEFAULT_EXTERNAL_KEY_ENV}
    # A keyless endpoint binds nothing, so the profile is not left unconfigured.
    assert rc.reviewer_profile_env(external)[1] == {}


def test_the_generator_keeps_the_default_key_only_as_presence(tmp_path):
    settings = gen.server_settings(
        [{rc.DEFAULT_EXTERNAL_KEY_ENV: "sk-live-value", "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://e.invalid/v1"}],
        rc.REVIEWER_PROFILE_SETTINGS, (rc.MCP_BEARER_ENV, rc.DEFAULT_EXTERNAL_KEY_ENV))
    assert settings[rc.DEFAULT_EXTERNAL_KEY_ENV] == "1"
    fixed, secrets = rc.reviewer_profile_env(settings)
    assert "sk-live-value" not in json.dumps([fixed, secrets])
    assert rc.DEFAULT_EXTERNAL_KEY_ENV in secrets



def test_the_reviewer_inherits_what_its_cli_allowlist_passes():
    from agents.dialectic_reviewer import host_backends
    assert set(gen.REVIEWER_INHERIT) == set(host_backends._CLI_ENV_ALLOWLIST) - {"USER"}
    profile = placeholder_document()["profiles"]["dialectic.reviewer"]
    assert profile["env"]["inherit"] == list(gen.REVIEWER_INHERIT)
    assert profile["env"]["set"]["USER"] == "operator"


# --- council lanes ---------------------------------------------------------

def _council(roots=("/srv/repos",)):
    return gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli="/a",
                              codex_cli="/b", antigravity_cli="/c",
                              council_roots=list(roots))["profiles"]


def test_council_lanes_need_a_root_except_agy():
    without = gen.build_profiles(repo_root="/r", python="/p", user="u", claude_cli="/a",
                                 codex_cli="/b", antigravity_cli="/c")["profiles"]
    assert [n for n in without if n.startswith("council.")] == ["council.agy"]
    assert {"council.claude", "council.codex", "council.agy"} <= set(_council())


def test_council_lanes_fix_the_lane_cli_and_roots():
    profiles = _council(["/srv/a", "/srv/b"])
    for lane, cli in (("claude", "/a"), ("codex", "/b"), ("agy", "/c")):
        profile = profiles[f"council.{lane}"]
        assert profile["args"] == ["-I", os.path.join("/r", gen.COUNCIL_CLIENT)]
        assert profile["env"]["set"]["COUNCIL_LANE"] == lane
        assert profile["env"]["set"]["COUNCIL_CLI"] == cli
        assert profile["lineage"] == "none" and profile["lease"] is False
        assert "cd" not in profile and "secrets" not in profile["env"]
    assert profiles["council.claude"]["env"]["set"]["COUNCIL_ROOTS"] == os.pathsep.join(
        ["/srv/a", "/srv/b"])
    assert "COUNCIL_ROOTS" not in profiles["council.agy"]["env"]["set"]


def test_council_variables_are_the_prompt_dir_and_model_only():
    profiles = _council()
    assert set(profiles["council.claude"]["vars"]) == {"COUNCIL_PROMPT", "COUNCIL_DIR"}
    assert set(profiles["council.codex"]["vars"]) == {"COUNCIL_PROMPT", "COUNCIL_DIR",
                                                      "COUNCIL_MODEL"}
    assert set(profiles["council.agy"]["vars"]) == {"COUNCIL_PROMPT", "COUNCIL_MODEL"}
    assert profiles["council.codex"]["vars"]["COUNCIL_DIR"]["required"] is True


def test_council_agy_prompt_cap_matches_its_client():
    from src.mcp_handlers.support import antigravity_cli_client as agy
    from src.mcp_handlers.support import council_lane_client as council
    overhead = len(agy.guard_prompt("").encode()) + len(council.AGY_TEXT_ONLY.encode())
    assert gen.COUNCIL_AGY_PROMPT_MAX_BYTES == council.AGY_ARG_LIMIT - overhead
    assert council.AGY_ARG_LIMIT == agy.PROMPT_BYTES_LIMIT


def test_council_model_pattern_matches_the_client():
    from src.mcp_handlers.support import council_lane_client as council
    assert gen.MODEL_PATTERN == council.MODEL_PATTERN.pattern


@pytest.mark.parametrize("root", ["/", "~", "HOME_PARENT"])
def test_a_council_root_holding_the_home_directory_is_refused(tmp_path, root):
    home = tmp_path / "home" / "op"
    home.mkdir(parents=True)
    entry = {"~": str(home), "HOME_PARENT": str(home.parent)}.get(root, root)
    with pytest.raises(SystemExit, match="contains the home directory"):
        gen.council_roots([entry], str(home))


def test_council_roots_resolve_symlinks_and_refuse_relative_or_missing(tmp_path):
    real = tmp_path / "repos"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home = str(tmp_path / "home")
    assert gen.council_roots([str(link), str(real)], home) == [os.path.realpath(real)]
    with pytest.raises(SystemExit, match="absolute"):
        gen.council_roots(["repos"], home)
    with pytest.raises(SystemExit, match="not a directory"):
        gen.council_roots([str(tmp_path / "missing")], home)


def test_main_writes_council_lanes_for_a_root(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    code, out, _ = _main(tmp_path, "--council-root", str(repos))
    assert code == 0
    profiles = json.loads(out.read_text())["profiles"]
    assert profiles["council.claude"]["env"]["set"]["COUNCIL_ROOTS"] == os.path.realpath(repos)
