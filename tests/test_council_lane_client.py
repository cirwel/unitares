"""The council.* launch profiles' client (council_lane_client.py).

Runs the client against a stand-in CLI that reports what it was given, so
each lane's command line, directory, environment and prompt route are
checked on a real child process.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.mcp_handlers.support import antigravity_cli_client as agy_client
from src.mcp_handlers.support import claude_cli_client as claude_client
from src.mcp_handlers.support import codex_app_server_client as codex_client
from src.mcp_handlers.support import council_lane_client as client

CLIENT = Path(client.__file__).resolve()

SHIM = """
import json, os, sys
data = sys.stdin.read() if not sys.stdin.isatty() else ""
print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
                  "stdin": data}))
sys.exit(int(data[len("exit:"):]) if data.startswith("exit:") else 0)
"""


@pytest.fixture
def shim(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "cli"
    path.parent.mkdir()
    path.write_text(f"#!{sys.executable}\n{SHIM}")
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def repos(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    (root / "project").mkdir(parents=True)
    return root


@pytest.fixture
def confined(monkeypatch):
    """The codex lane's admin-config and sandbox-probe checks pass."""
    monkeypatch.setattr(codex_client, "admin_config_conflict", lambda paths=None: None)
    monkeypatch.setattr(codex_client, "confinement_verified", lambda cli: True)
    modules = {"codex_app_server_client": codex_client,
               "claude_cli_client": claude_client,
               "antigravity_cli_client": agy_client}
    monkeypatch.setattr(client, "_sibling", modules.__getitem__)


def _run(environ: dict[str, str], capfd) -> tuple[int, dict | None, str]:
    err = io.StringIO()
    code = client.run(environ, err)
    out = capfd.readouterr().out.strip()
    return code, (json.loads(out.splitlines()[-1]) if out else None), err.getvalue()


def _env(lane: str, shim: str, repos: Path, **extra: str) -> dict[str, str]:
    return {"COUNCIL_LANE": lane, "COUNCIL_CLI": shim, "COUNCIL_PROMPT": "review this",
            "COUNCIL_ROOTS": str(repos), "COUNCIL_DIR": str(repos / "project"),
            "PATH": os.environ["PATH"], "HOME": str(repos.parent / "home"),
            "TMPDIR": str(repos.parent), **extra}


# --- claude ----------------------------------------------------------------

def test_claude_runs_confined_in_the_directory_with_the_prompt_on_stdin(
        shim, repos, capfd, confined):
    code, seen, _ = _run(_env("claude", shim, repos, COUNCIL_PROMPT="--dangerously-skip"),
                         capfd)
    assert code == 0
    assert seen["cwd"] == os.path.realpath(repos / "project")
    assert seen["stdin"] == "--dangerously-skip"
    assert "--dangerously-skip" not in seen["argv"]
    assert seen["argv"] == list(client.CLAUDE_FLAGS)
    for flag in ("--safe-mode", "--restricted", "--strict-mcp-config"):
        assert flag in seen["argv"]
    assert seen["argv"][seen["argv"].index("--permission-mode") + 1] == "dontAsk"
    assert seen["argv"][seen["argv"].index("--tools") + 1] == "Read,Grep,Glob"


def test_claude_denies_credential_files_inside_the_directory():
    flags = list(client.CLAUDE_FLAGS)
    denied = flags[flags.index("--disallowedTools") + 1:flags.index("--permission-mode")]
    assert denied == list(client.CLAUDE_DENIED_READS)
    assert any(".env" in rule for rule in denied) and any(".unitares" in r for r in denied)


def test_claude_environment_is_allowlisted(shim, repos, capfd, confined):
    code, seen, _ = _run(_env("claude", shim, repos, ANTHROPIC_API_KEY="x",
                              UNITARES_MCP_BEARER_TOKEN="y", NODE_OPTIONS="--require=z"),
                         capfd)
    assert code == 0
    for name in ("ANTHROPIC_API_KEY", "UNITARES_MCP_BEARER_TOKEN", "NODE_OPTIONS",
                 "COUNCIL_PROMPT", "COUNCIL_ROOTS"):
        assert name not in seen["env"]
    assert "USER" in seen["env"]


# --- the directory ---------------------------------------------------------

@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_directory_outside_the_roots_is_refused(shim, repos, tmp_path, capfd, confined,
                                                   lane):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    code, seen, err = _run(_env(lane, shim, repos, COUNCIL_DIR=str(outside)), capfd)
    assert (code, seen) == (client.EXIT_BAD_INPUT, None)
    assert "outside the profile's roots" in err


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_symlink_out_of_the_roots_is_refused(shim, repos, tmp_path, capfd, confined, lane):
    outside = tmp_path / "secret"
    outside.mkdir()
    (repos / "escape").symlink_to(outside)
    code, seen, err = _run(_env(lane, shim, repos, COUNCIL_DIR=str(repos / "escape")), capfd)
    assert (code, seen) == (client.EXIT_BAD_INPUT, None)
    assert "outside the profile's roots" in err


def test_a_sibling_with_the_root_as_prefix_is_outside(shim, repos, capfd, confined):
    sibling = Path(str(repos) + "-other")
    sibling.mkdir()
    code, _, err = _run(_env("claude", shim, repos, COUNCIL_DIR=str(sibling)), capfd)
    assert code == client.EXIT_BAD_INPUT and "outside" in err


@pytest.mark.parametrize("value, message", [
    ("relative/path", "absolute"),
    ("", "absolute"),
])
def test_a_bad_directory_value_is_refused(shim, repos, capfd, confined, value, message):
    code, _, err = _run(_env("claude", shim, repos, COUNCIL_DIR=value), capfd)
    assert code == client.EXIT_BAD_INPUT and message in err


def test_a_directory_with_glob_characters_is_refused(shim, repos, capfd, confined):
    globby = repos / "pro[ject]"
    globby.mkdir()
    code, _, err = _run(_env("codex", shim, repos, COUNCIL_DIR=str(globby)), capfd)
    assert code == client.EXIT_BAD_INPUT and "glob" in err


@pytest.mark.parametrize("lane", ["claude", "codex"])
@pytest.mark.parametrize("protected", [".unitares", ".unitares/sub", ".env", ".envs/x",
                                       "prod.env"])
def test_a_protected_directory_cannot_be_the_directory(shim, repos, capfd, confined, lane,
                                                       protected):
    """The lanes deny these beneath the directory; naming one, or a path
    inside one, as the directory would grant all of it (#3102 review)."""
    target = repos / "project" / protected
    target.mkdir(parents=True)
    code, seen, err = _run(_env(lane, shim, repos, COUNCIL_DIR=str(target)), capfd)
    assert (code, seen) == (client.EXIT_BAD_INPUT, None)
    assert ".unitares" in err


def test_a_symlink_into_a_protected_directory_is_refused(shim, repos, capfd, confined):
    cache = repos / "project" / ".unitares"
    cache.mkdir()
    (repos / "project" / "cache-link").symlink_to(cache)
    code, _, err = _run(_env("claude", shim, repos,
                             COUNCIL_DIR=str(repos / "project" / "cache-link")), capfd)
    assert code == client.EXIT_BAD_INPUT and ".unitares" in err


def test_a_lane_without_roots_refuses(shim, repos, capfd, confined):
    code, _, err = _run(_env("claude", shim, repos, COUNCIL_ROOTS=""), capfd)
    assert code == client.EXIT_BAD_INPUT and "COUNCIL_ROOTS" in err


# --- codex -----------------------------------------------------------------

def test_codex_runs_in_an_empty_directory_and_reads_the_repo_by_path(
        shim, repos, capfd, confined):
    code, seen, _ = _run(_env("codex", shim, repos), capfd)
    assert code == 0
    project = os.path.realpath(repos / "project")
    assert seen["cwd"] != project and not os.path.exists(seen["cwd"])
    argv = seen["argv"]
    assert argv[:2] == ["exec", "--ignore-user-config"] and argv[-1] == "-"
    assert argv[argv.index("-C") + 1] == seen["cwd"]
    assert "project_doc_max_bytes=0" in argv
    table = next(a for a in argv if a.startswith("permissions.") and ".filesystem=" in a)
    assert f'"{project}"="read"' in table
    assert f'"{project}/**/.env*"="deny"' in table
    assert f'"{project}/.unitares"="deny"' in table
    assert '":root"="deny"' in table
    assert seen["stdin"].startswith(f"The repository under review is the directory {project}.")
    assert seen["stdin"].endswith("review this")


def test_codex_reads_a_worktrees_git_directories(shim, repos, capfd, confined):
    project = repos / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    code, seen, _ = _run(_env("codex", shim, repos), capfd)
    assert code == 0
    table = next(a for a in seen["argv"] if ".filesystem=" in a)
    assert f'"{os.path.realpath(project / ".git")}"="read"' in table


def test_codex_model_choice_and_user_config_fallback(shim, repos, tmp_path, capfd, confined):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n[mcp_servers.x]\nmodel = "no"\n')
    _, seen, _ = _run(_env("codex", shim, repos, CODEX_HOME=str(codex_home)), capfd)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert 'model_reasoning_effort="high"' in argv
    assert seen["env"]["CODEX_HOME"] == str(codex_home)
    _, seen, _ = _run(_env("codex", shim, repos, CODEX_HOME=str(codex_home),
                           COUNCIL_MODEL="gpt-6"), capfd)
    assert seen["argv"][seen["argv"].index("--model") + 1] == "gpt-6"


@pytest.mark.parametrize("lane", ["codex", "agy"])
def test_a_model_that_could_read_as_an_option_is_refused(shim, repos, capfd, confined, lane):
    code, _, err = _run(_env(lane, shim, repos, COUNCIL_MODEL="--yolo"), capfd)
    assert code == client.EXIT_BAD_INPUT and "COUNCIL_MODEL" in err


def test_codex_refuses_without_confinement(shim, repos, capfd, confined, monkeypatch):
    monkeypatch.setattr(codex_client, "confinement_verified", lambda cli: False)
    code, seen, err = _run(_env("codex", shim, repos), capfd)
    assert (code, seen) == (client.EXIT_NOT_CONFINED, None)
    assert "sandbox probe" in err
    monkeypatch.setattr(codex_client, "admin_config_conflict", lambda paths=None: "/etc/x")
    code, _, err = _run(_env("codex", shim, repos), capfd)
    assert code == client.EXIT_NOT_CONFINED and "administrator" in err


def test_codex_refuses_a_directory_under_a_shared_temp_root(
        shim, repos, capfd, confined, monkeypatch):
    """codex's lane denies the shared temp roots, so it could not read a
    directory there: refused with the reason, before anything runs."""
    monkeypatch.setattr(codex_client, "TEMP_ROOTS", (str(repos),))
    code, seen, err = _run(_env("codex", shim, repos), capfd)
    assert (code, seen) == (client.EXIT_BAD_INPUT, None)
    assert "shared temporary directory" in err


def test_codex_refuses_a_working_directory_under_a_shared_temp_root(
        shim, repos, tmp_path, capfd, confined, monkeypatch):
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(client.tempfile, "tempdir", str(shared))
    monkeypatch.setattr(codex_client, "TEMP_ROOTS", (str(shared),))
    code, seen, err = _run(_env("codex", shim, repos), capfd)
    assert (code, seen) == (client.EXIT_NOT_CONFINED, None)
    assert "TMPDIR" in err
    assert list(shared.iterdir()) == []


# --- agy -------------------------------------------------------------------

def test_agy_guards_the_prompt_and_isolates_home(shim, repos, capfd, confined):
    code, seen, _ = _run(_env("agy", shim, repos, COUNCIL_PROMPT="/skill run",
                              COUNCIL_DIR="", COUNCIL_ROOTS=""), capfd)
    assert code == 0
    argv = seen["argv"]
    assert argv[0] == "-p"
    assert argv[1].startswith(agy_client.PROMPT_GUARD)
    assert argv[1].endswith(client.AGY_TEXT_ONLY)
    assert argv[2:] == ["--mode", "plan", "--sandbox", "--output-format", "stream-json"]
    assert seen["env"]["HOME"] != str(repos.parent / "home")
    assert os.path.basename(seen["cwd"]) == "workspace"
    assert not os.path.exists(seen["cwd"])


def test_agy_refuses_a_prompt_over_its_argument_limit(shim, repos, capfd, confined):
    code, _, err = _run(_env("agy", shim, repos, COUNCIL_PROMPT="x" * client.AGY_ARG_LIMIT),
                        capfd)
    assert code == client.EXIT_BAD_INPUT and "too long" in err


# --- common ----------------------------------------------------------------

@pytest.mark.parametrize("environ, message, expected", [
    ({"COUNCIL_LANE": "gemini"}, "COUNCIL_LANE", client.EXIT_BAD_INPUT),
    ({"COUNCIL_PROMPT": "  "}, "empty", client.EXIT_BAD_INPUT),
    ({"COUNCIL_CLI": "cli"}, "absolute", client.EXIT_BAD_INPUT),
    ({"COUNCIL_CLI": "/nonexistent/cli"}, "not an executable", client.EXIT_NO_CLI),
])
def test_bad_input_is_refused_before_anything_runs(shim, repos, capfd, confined, environ,
                                                   message, expected):
    code, seen, err = _run({**_env("claude", shim, repos), **environ}, capfd)
    assert (code, seen) == (expected, None)
    assert message in err


def test_exit_status_passes_through(shim, repos, capfd, confined):
    code, _, _ = _run(_env("claude", shim, repos, COUNCIL_PROMPT="exit:7"), capfd)
    assert code == 7


@pytest.mark.parametrize("lane", ["claude", "codex", "agy"])
def test_runs_under_isolated_mode_loading_its_siblings_by_path(shim, repos, lane, tmp_path):
    """As the profile starts it: ``python -I`` leaves the client's directory
    off sys.path, so the sibling clients must load by path. codex is refused
    by its real sandbox probe against the shim, which shows the sibling
    loaded and ran."""
    env = _env(lane, shim, repos)
    proc = subprocess.run([sys.executable, "-I", str(CLIENT)], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    if lane == "codex":
        assert proc.returncode == client.EXIT_NOT_CONFINED, proc.stderr
        assert "sandbox probe" in proc.stderr or "administrator" in proc.stderr
    else:
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout.splitlines()[-1])["argv"]
