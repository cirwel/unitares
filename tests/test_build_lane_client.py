"""The build.claude launch profile's client (build_lane_client.py).

Runs the client against a stand-in CLI that reports what it was given, so
the command line, directory confinement, environment, shims and result file
are checked on a real child process.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.mcp_handlers.support import build_lane_client as client
from src.mcp_handlers.support import claude_cli_client

CLIENT = Path(client.__file__).resolve()

# The stand-in reads the brief on stdin, reports as stream-json events, and
# steers itself by the brief's first line.
SHIM = """
import json, os, subprocess, sys, time
data = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1",
                  "model": "stand-in"}))
report = {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "stdin": data}
if data.startswith("git:"):
    for args in (["push"], ["add", "-f", "x"], ["remote", "-v"], ["status", "--short"],
                 ["-C", ".", "push", "origin"]):
        proc = subprocess.run(["git", *args], capture_output=True, text=True)
        report.setdefault("git", {})[" ".join(args)] = [proc.returncode, proc.stderr.strip()]
    for name in ("gh", "unitares-github"):
        proc = subprocess.run([name, "pr", "merge"], capture_output=True, text=True)
        report.setdefault("git", {})[name] = [proc.returncode, proc.stderr.strip()]
if data.startswith("sleep:"):
    print(json.dumps({"type": "assistant", "report": report}), flush=True)
    time.sleep(float(data.split(":")[1]))
print(json.dumps({"type": "result", "is_error": False, "result": "done:" + data[:20],
                  "num_turns": 3, "total_cost_usd": 0.0, "report": report}))
sys.exit(int(data[len("exit:"):].split()[0]) if data.startswith("exit:") else 0)
"""


@pytest.fixture
def shim(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(f"#!{sys.executable}\n{SHIM}")
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "build").mkdir(parents=True)
    return root


@pytest.fixture
def brief(tmp_path: Path) -> Path:
    path = tmp_path / "BRIEF.md"
    path.write_text("Build the kit.\n")
    path.chmod(0o600)
    return path


def _environ(shim: str, roots: Path, brief: Path, **extra: str) -> dict[str, str]:
    environ = {
        "BUILD_CLI": shim, "BUILD_ROOTS": str(roots), "BUILD_BRIEF": str(brief),
        "BUILD_DIR": str(roots / "build"), "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/"), "AGENT_ORCHESTRATOR_BEARER_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "metered", "UNITARES_PARENT_AGENT_ID": "p-1",
        "UNITARES_SPAWN_REASON": "build_request",
    }
    environ.update(extra)
    return environ


def _run(environ: dict[str, str]) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    code = client.run(environ, out, err)
    report = None
    for line in out.getvalue().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            report = event["report"]
    return code, report, err.getvalue()


def test_runs_the_cli_in_the_directory_with_the_brief_on_stdin(shim, roots, brief):
    code, report, _ = _run(_environ(shim, roots, brief))
    assert code == 0
    assert report["stdin"] == "Build the kit.\n"
    assert report["cwd"] == os.path.realpath(roots / "build")
    argv = report["argv"]
    assert argv[:2] == ["-p", "--strict-mcp-config"]
    assert "--safe-mode" not in argv and "--restricted" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--tools") + 1] == ",".join(client.TOOLS)
    assert "Bash(git push*)" in argv and "Bash(gh *)" in argv
    assert "--mcp-config" not in argv and "--model" not in argv


def test_environment_is_allowlisted_and_lineage_passes_through(shim, roots, brief):
    _, report, _ = _run(_environ(shim, roots, brief))
    env = report["env"]
    assert "AGENT_ORCHESTRATOR_BEARER_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["UNITARES_PARENT_AGENT_ID"] == "p-1"
    assert env["UNITARES_SPAWN_REASON"] == "build_request"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "USER" in env
    allowed = {*claude_cli_client.ENV_ALLOWLIST, *client.LINEAGE_PASSTHROUGH, *client.GIT_ENV,
               "BUILD_LANE_REAL_GIT", "PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING",
               "OLDPWD", "LC_CTYPE"}
    assert set(env) <= allowed, set(env) - allowed


def test_git_push_gh_and_force_add_are_refused_by_shims(shim, roots, brief):
    subprocess.run(["git", "init", "-q", str(roots / "build")], check=True)
    brief.write_text("git:\n")
    _, report, _ = _run(_environ(shim, roots, brief))
    results = report["git"]
    assert results["push"][0] == 3 and "caller publishes" in results["push"][1]
    assert results["add -f x"][0] == 3
    assert results["remote -v"][0] == 3
    assert results["-C . push origin"][0] == 3
    assert results["status --short"][0] == 0
    assert results["gh"][0] == 3 and results["unitares-github"][0] == 3


def test_model_mcp_config_and_read_roots_are_passed(shim, roots, brief, tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text("{}")
    docs = tmp_path / "docs"
    docs.mkdir()
    _, report, _ = _run(_environ(shim, roots, brief, BUILD_MODEL="opus",
                                 BUILD_MCP_CONFIG=str(config), BUILD_READ_ROOTS=str(docs)))
    argv = report["argv"]
    assert argv[argv.index("--mcp-config") + 1] == str(config)
    assert argv[argv.index("--model") + 1] == "opus"
    real_docs = os.path.realpath(docs)
    assert argv[argv.index("--add-dir") + 1] == real_docs
    # A read root stays read-only for the file tools: deny rules under it.
    # Two leading slashes: an absolute rule path (one would be project-relative).
    for tool in ("Edit", "Write", "NotebookEdit"):
        assert f"{tool}(/{real_docs}/**)" in argv
    assert argv.index(f"Edit(/{real_docs}/**)") > argv.index("--disallowedTools")


def test_a_refusal_after_the_directory_is_known_replaces_the_old_result(shim, roots, brief):
    state = roots / "build" / client.STATE_DIR
    state.mkdir()
    (state / "result.json").write_text('{"exit": 0, "stale": true}')
    brief.write_text("   \n")  # the brief became empty since the last run
    code, report, err = _run(_environ(shim, roots, brief))
    assert code == 2 and report is None and "empty" in err
    result = json.loads((state / "result.json").read_text())
    assert result["exit"] == 2 and "stale" not in result and "empty" in result["refusal"]


def test_a_brief_larger_than_the_pipe_does_not_deadlock(shim, roots, brief):
    # The stand-in prints before it reads; a brief past the pipe buffer used
    # to block the write while the CLI blocked on its full stdout.
    brief.write_text("x" * 300_000 + "\n")
    code, report, _ = _run(_environ(shim, roots, brief, BUILD_DEADLINE_S="30"))
    assert code == 0
    assert len(report["stdin"]) == 300_001


def test_an_earlier_runs_result_is_removed_before_the_cli_starts(shim, roots, brief):
    state = roots / "build" / client.STATE_DIR
    state.mkdir()
    (state / "result.json").write_text('{"exit": 0, "stale": true}')
    (state / "build.log").write_text("stale-log-line\n")
    brief.write_text("sleep:2\n")
    import threading
    seen: dict = {}

    def watch() -> None:
        # While the CLI runs, the old result must already be gone.
        import time
        for _ in range(40):
            if (state / "started.json").exists():
                seen["result_during_run"] = (state / "result.json").exists()
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch)
    watcher.start()
    code, _, _ = _run(_environ(shim, roots, brief))
    watcher.join()
    assert code == 0
    assert seen.get("result_during_run") is False
    assert "stale" not in (state / "result.json").read_text()
    assert "stale-log-line" not in (state / "build.log").read_text()


def test_writes_started_log_and_result_files(shim, roots, brief):
    brief.write_text("exit:7 done\n")
    code, _, _ = _run(_environ(shim, roots, brief))
    assert code == 7
    state = roots / "build" / client.STATE_DIR
    started = json.loads((state / "started.json").read_text())
    assert started["parent_agent_id"] == "p-1"
    result = json.loads((state / "result.json").read_text())
    assert result["exit"] == 7 and result["killed"] is None
    assert result["result_text"].startswith("done:exit:7")
    assert result["model"] == "stand-in" and result["num_turns"] == 3
    assert "ended" in result
    assert '"type": "result"' in (state / "build.log").read_text()


def test_the_deadline_stops_the_cli_and_still_writes_the_result(shim, roots, brief):
    brief.write_text("sleep:30\n")
    code, _, err = _run(_environ(shim, roots, brief, BUILD_DEADLINE_S="1"))
    assert code == client.EXIT_DEADLINE
    assert "deadline" in err
    result = json.loads((roots / "build" / client.STATE_DIR / "result.json").read_text())
    assert result["killed"] == "deadline" and result["exit"] == client.EXIT_DEADLINE


@pytest.mark.parametrize("value, message", [
    ("relative/dir", "absolute"),
    ("/nonexistent/dir", "not a directory"),
])
def test_a_bad_directory_is_refused(shim, roots, brief, value, message):
    code, report, err = _run(_environ(shim, roots, brief, BUILD_DIR=value))
    assert code == 2 and report is None and message in err


def test_a_directory_outside_the_roots_is_refused(shim, roots, brief, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    code, _, err = _run(_environ(shim, roots, brief, BUILD_DIR=str(outside)))
    assert code == 2 and "outside" in err


def test_a_symlink_out_of_the_roots_is_refused(shim, roots, brief, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (roots / "escape").symlink_to(outside)
    code, _, err = _run(_environ(shim, roots, brief, BUILD_DIR=str(roots / "escape")))
    assert code == 2 and "outside" in err


def test_a_protected_directory_is_refused(shim, roots, brief):
    (roots / ".unitares").mkdir()
    code, _, err = _run(_environ(shim, roots, brief, BUILD_DIR=str(roots / ".unitares")))
    assert code == 2 and ".unitares" in err


def test_a_lane_without_roots_or_brief_refuses(shim, roots, brief):
    code, _, err = _run(_environ(shim, roots, brief, BUILD_ROOTS=""))
    assert code == 2 and "BUILD_ROOTS" in err
    brief.write_text("   \n")
    code, _, err = _run(_environ(shim, roots, brief))
    assert code == 2 and "empty" in err


def test_a_brief_others_can_write_is_refused(shim, roots, brief):
    brief.chmod(0o666)
    code, _, err = _run(_environ(shim, roots, brief))
    assert code == 2 and "writable" in err


def test_a_model_that_could_read_as_an_option_is_refused(shim, roots, brief):
    code, _, err = _run(_environ(shim, roots, brief, BUILD_MODEL="--yolo"))
    assert code == 2 and "BUILD_MODEL" in err


def test_a_missing_cli_is_exit_127(roots, brief, tmp_path):
    code, _, err = _run(_environ(str(tmp_path / "no-cli"), roots, brief))
    assert code == 127 and "not an executable" in err


def test_runs_under_isolated_mode_loading_its_sibling_by_path(shim, roots, brief):
    env = {k: v for k, v in _environ(shim, roots, brief).items()}
    proc = subprocess.run([sys.executable, "-I", str(CLIENT)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert '"type": "result"' in proc.stdout
