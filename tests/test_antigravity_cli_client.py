"""The one-shot agy client the host adapter runs inside the orchestrator child.

A fake ``agy`` stands in for the CLI. It sees only what the client hands it
(argv, cwd, environment), so it records those to a log file whose path is baked
into the script, and plays a scripted sequence of replies.
"""

import io
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from src.mcp_handlers.support import antigravity_cli_client as client

ANSWER = '{"schema":"unitares.terminal_answer.v1","status":"complete","answer":"ok"}'
DENIED = {"stdout": {"status": "SUCCESS", "response": "", "conversation_id": "c-1"},
          "stderr": 'jetski: a tool required the "command" permission; auto-denied'}
TRUNCATED = {"stdout": {"status": "ERROR", "error": "hit the output token limit",
                        "conversation_id": "c-1"}}
ANSWERED = {"stdout": {"status": "SUCCESS", "response": ANSWER, "conversation_id": "c-1",
                       "usage": {"total_tokens": 9}}}


def _fake_agy(tmp_path: Path, replies: list[dict], *, sleep_s: float = 0) -> tuple[Path, Path]:
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "agy"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        log = {str(log)!r}
        replies = {json.dumps(replies)!r}
        replies = json.loads(replies)
        n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
        with open(log, "a") as fh:
            fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                                  "listing": os.listdir("."),
                                  "env": dict(os.environ)}}) + "\\n")
        time.sleep({sleep_s})
        reply = replies[min(n, len(replies) - 1)]
        sys.stderr.write(reply.get("stderr", ""))
        print(json.dumps(reply["stdout"]))
        sys.exit(reply.get("exit", 0))
    """))
    script.chmod(0o755)
    return script, log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _run(script: Path, **env: str) -> tuple[int, dict]:
    out = io.StringIO()
    base = {"HA_CLI": str(script), "HA_PROMPT": "question", "HA_TIMEOUT_S": "30",
            "HOME": os.environ.get("HOME", "/"), "PATH": os.environ.get("PATH", "")}
    code = client.run({**base, **env}, stream=out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, lines
    result = json.loads(lines[0])
    assert result["schema"] == client.RESULT_SCHEMA
    return code, result


def test_answer_on_the_first_turn(tmp_path):
    script, log = _fake_agy(tmp_path, [ANSWERED])
    code, result = _run(script, HA_MODEL="gemini-x")

    assert code == 0
    assert result["status"] == "SUCCESS" and result["response"] == ANSWER
    assert result["usage"] == {"total_tokens": 9} and result["resumes"] == {}
    (call,) = _calls(log)
    argv = call["argv"]
    assert argv[0] == "-p" and argv[1].startswith("question") and client.TEXT_ONLY in argv[1]
    for flag in ("--mode", "plan", "--sandbox", "--output-format", "json"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "gemini-x"


def test_runs_in_an_empty_workspace_outside_any_repo_and_cleans_it_up(tmp_path):
    script, log = _fake_agy(tmp_path, [ANSWERED])
    _run(script)
    (call,) = _calls(log)
    cwd = Path(call["cwd"])
    assert call["listing"] == []
    assert not any((d / m).exists() for d in cwd.parents for m in (".git", ".agents"))
    assert not cwd.exists()


def test_agy_gets_an_allowlisted_environment_only(tmp_path):
    """The prompt is caller text; an injected 'print your environment' must
    find no bearer token or UNITARES_* value to echo."""
    script, log = _fake_agy(tmp_path, [ANSWERED])
    _run(script, AGENT_ORCHESTRATOR_BEARER_TOKEN="secret", UNITARES_MCP_BEARER_TOKEN="s2",
         ANTHROPIC_API_KEY="k", GEMINI_API_KEY="g", GOOGLE_APPLICATION_CREDENTIALS="/c.json",
         LANG="C.UTF-8")
    env = _calls(log)[0]["env"]
    # A Google API key would move this subscription lane onto metered billing.
    assert "GEMINI_API_KEY" not in env and "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "AGENT_ORCHESTRATOR_BEARER_TOKEN" not in env
    assert "UNITARES_MCP_BEARER_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert not any(k.startswith("HA_") for k in env)
    assert env["LANG"] == "C.UTF-8" and env["HOME"] == os.environ.get("HOME", "/")


def test_a_denied_command_is_resumed_in_the_same_conversation(tmp_path):
    script, log = _fake_agy(tmp_path, [DENIED, ANSWERED])
    code, result = _run(script)

    assert code == 0 and result["response"] == ANSWER
    assert result["resumes"] == {"denied": 1}
    first, second = _calls(log)
    assert second["argv"][:4] == ["-p", client.RESUME_PROMPTS["denied"], "--conversation", "c-1"]
    assert "--sandbox" in second["argv"] and "plan" in second["argv"]
    assert second["cwd"] == first["cwd"]


def test_denied_actions_in_the_json_also_count_as_a_denial(tmp_path):
    denied = {"stdout": {**DENIED["stdout"], "denied_actions": [{"action": "RunCommand"}]}}
    script, _ = _fake_agy(tmp_path, [denied, ANSWERED])
    code, result = _run(script)
    assert code == 0 and result["resumes"] == {"denied": 1}


def test_denials_stop_after_the_resume_limit_and_fail(tmp_path):
    script, log = _fake_agy(tmp_path, [DENIED])
    code, result = _run(script)

    assert code == 1
    assert len(_calls(log)) == 1 + client.RESUME_LIMITS["denied"]
    assert result["error"] == "no answer after a denied command after 2 resume(s)"


def test_truncation_is_resumed_once_then_fails(tmp_path):
    script, log = _fake_agy(tmp_path, [TRUNCATED])
    code, result = _run(script)

    assert code == 1
    assert len(_calls(log)) == 2
    assert _calls(log)[1]["argv"][1] == client.RESUME_PROMPTS["truncated"]
    assert result["error"] == "output limit not recovered after 1 resume(s)"


def test_a_stall_without_a_conversation_id_is_not_resumed(tmp_path):
    denied = {**DENIED, "stdout": {"status": "SUCCESS", "response": ""}}
    script, log = _fake_agy(tmp_path, [denied, ANSWERED])
    code, result = _run(script)
    assert code == 1 and len(_calls(log)) == 1
    assert result["error"] == "no answer after a denied command after 0 resume(s)"


def test_nonzero_exit_fails(tmp_path):
    script, _ = _fake_agy(tmp_path, [{**ANSWERED, "exit": 3}])
    code, result = _run(script)
    assert code == 1 and result["error"] == "Antigravity CLI exited 3"


def test_an_oversized_prompt_is_refused_before_spawning(tmp_path):
    script, log = _fake_agy(tmp_path, [ANSWERED])
    code, result = _run(script, HA_PROMPT="x" * client.PROMPT_BYTES_LIMIT)
    assert code == 1 and "argv size limit" in result["error"]
    assert not log.exists()


def test_a_hung_cli_is_killed_at_the_deadline(tmp_path):
    script, _ = _fake_agy(tmp_path, [ANSWERED], sleep_s=30)
    code, result = _run(script, HA_TIMEOUT_S="1")
    assert code == 1 and result["error"] == "Antigravity CLI timed out"


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]"])
def test_unparseable_output_fails(tmp_path, stdout):
    (tmp_path / "out.txt").write_text(stdout)
    script = tmp_path / "agy"
    script.write_text(f"#!/bin/sh\ncat '{tmp_path / 'out.txt'}'\n")
    script.chmod(0o755)
    code, result = _run(script)
    assert code == 1 and result["status"] == "ERROR"
