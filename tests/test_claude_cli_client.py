"""The host.claude launch profile's client (claude_cli_client.py).

Runs the client against a stand-in CLI that reports what it was given, so
each claim in the module docstring is checked on a real child process.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from src.mcp_handlers.support import claude_cli_client as client

SHIM = """
import json, os, sys, time
data = sys.stdin.read()
# The environment is allowlisted, so the test steers the shim by its prompt.
if data.startswith("sleep:"):
    time.sleep(float(data[len("sleep:"):]))
print(json.dumps({"type": "result", "is_error": False, "result": data,
                  "argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}))
sys.exit(int(data[len("exit:"):]) if data.startswith("exit:") else 0)
"""


@pytest.fixture
def shim(tmp_path: Path) -> str:
    path = tmp_path / "claude"
    path.write_text(f"#!{sys.executable}\n{SHIM}")
    path.chmod(0o700)
    return str(path)


def _run(environ: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = client.run(environ, out, err)
    return code, out.getvalue(), err.getvalue()


def _base(shim: str, **extra: str) -> dict[str, str]:
    return {"HA_CLI": shim, "HA_PROMPT": "hello", "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/"), **extra}


def test_prompt_goes_on_stdin_and_a_dash_prompt_stays_text(shim):
    code, out, _ = _run(_base(shim, HA_PROMPT="--dangerously-skip-permissions"))
    assert code == 0
    echoed = json.loads(out)
    assert echoed["result"] == "--dangerously-skip-permissions"
    assert echoed["argv"] == list(client.FIXED_FLAGS)


def test_model_is_the_only_caller_option(shim):
    code, out, _ = _run(_base(shim, HA_MODEL="claude-opus-5-5"))
    assert code == 0
    assert json.loads(out)["argv"] == [*client.FIXED_FLAGS, "--model", "claude-opus-5-5"]


@pytest.mark.parametrize("model", ["-x", "--model", "a b", "x" * 129, "a;b"])
def test_a_model_that_could_read_as_an_option_is_refused(shim, model):
    code, out, err = _run(_base(shim, HA_MODEL=model))
    assert (code, out) == (2, "")
    assert "HA_MODEL" in err
    assert model not in err


def test_environment_is_allowlisted(shim):
    environ = _base(shim, ANTHROPIC_API_KEY="metered", ANTHROPIC_AUTH_TOKEN="t",
                    ANTHROPIC_BASE_URL="https://elsewhere.example",
                    CLAUDE_CODE_USE_BEDROCK="1", AGENT_ORCHESTRATOR_BEARER_TOKEN="b",
                    USER="operator")
    code, out, _ = _run(environ)
    assert code == 0
    env = json.loads(out)["env"]
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_USE_BEDROCK", "AGENT_ORCHESTRATOR_BEARER_TOKEN",
                 "HA_PROMPT", "HA_CLI"):
        assert name not in env
    assert env["USER"] == "operator"


def test_user_is_filled_in_when_the_service_omits_it(shim, monkeypatch):
    monkeypatch.setattr(client.getpass, "getuser", lambda: "looked-up")
    environ = _base(shim)
    environ.pop("USER", None)
    code, out, _ = _run(environ)
    assert code == 0
    assert json.loads(out)["env"]["USER"] == "looked-up"


def test_runs_in_an_empty_directory_that_is_removed(shim):
    code, out, _ = _run(_base(shim))
    assert code == 0
    cwd = json.loads(out)["cwd"]
    assert "unitares-claude-lane-" in cwd
    assert not os.path.exists(cwd)


def test_refuses_a_directory_inside_a_project(shim, tmp_path, monkeypatch):
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(client.tempfile, "tempdir", str(project))
    code, out, err = _run(_base(shim))
    assert (code, out) == (2, "")
    assert "inside a project" in err


def test_exit_status_passes_through(shim):
    code, out, _ = _run(_base(shim, HA_PROMPT="exit:3"))
    assert code == 3
    assert json.loads(out)["type"] == "result"


def test_deadline_kills_the_cli(shim, monkeypatch):
    monkeypatch.setattr(client, "KILL_GRACE_S", 1)
    code, _, err = _run(_base(shim, HA_TIMEOUT_S="1", HA_PROMPT="sleep:30"))
    assert code == 124
    assert "timed out after 1s" in err


@pytest.mark.parametrize(
    "environ, message, expected",
    [
        ({"HA_PROMPT": "  "}, "HA_PROMPT is empty", 2),
        ({"HA_CLI": "claude"}, "absolute path", 2),
        ({"HA_CLI": "/nonexistent/claude"}, "not an executable", 127),
        ({"HA_TIMEOUT_S": "0"}, "HA_TIMEOUT_S", 2),
        ({"HA_TIMEOUT_S": "1e3"}, "HA_TIMEOUT_S", 2),
        ({"HA_TIMEOUT_S": str(client.MAX_TIMEOUT_S + 1)}, "HA_TIMEOUT_S", 2),
    ],
)
def test_bad_input_is_refused_before_anything_runs(shim, environ, message, expected):
    code, out, err = _run({**_base(shim), **environ})
    assert (code, out) == (expected, "")
    assert message in err
