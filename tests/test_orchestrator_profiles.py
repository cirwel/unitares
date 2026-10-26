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


def test_fixture_is_the_generator_output_for_placeholder_paths():
    document = gen.build_profiles(repo_root="/srv/unitares", python="/usr/bin/python3",
                                  user="operator", claude_cli="/usr/local/bin/claude",
                                  codex_cli="/usr/local/bin/codex",
                                  antigravity_cli="/usr/local/bin/agy")
    assert FIXTURE.read_text(encoding="utf-8") == gen.render(document)


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
        assert len("HA_PROMPT=") + profile["vars"]["HA_PROMPT"]["max_bytes"] + 1 <= 131_072


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
