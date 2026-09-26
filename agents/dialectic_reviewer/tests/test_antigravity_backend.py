"""Antigravity (agy) dialectic backend: empty workspace, JSON parse, fallback."""

import asyncio
import json
from pathlib import Path

from agents.dialectic_reviewer import host_backends as hb

VERDICT = {"agrees": False, "root_cause": "r", "proposed_conditions": ["c"], "reasoning": "x"}


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout, self.returncode, self.killed = stdout, returncode, False

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        self.killed = True


def _spawn(monkeypatch, stdout: bytes, returncode: int = 0, seen: dict | None = None):
    async def fake_exec(*argv, cwd=None, **kw):
        if seen is not None:
            seen["argv"], seen["cwd"], seen["kw"] = argv, cwd, kw
            seen["listing"] = sorted(Path(cwd).iterdir())
        return _Proc(stdout, returncode)

    monkeypatch.setattr(hb, "resolve_antigravity_cli", lambda: "/Users/op/.local/bin/agy")
    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec", fake_exec)


def test_runs_read_only_from_an_empty_workspace_and_parses_the_verdict(monkeypatch):
    seen = {}
    out = {"status": "SUCCESS", "response": "thinking…\n" + json.dumps(VERDICT),
           "usage": {"total_tokens": 1234}}
    _spawn(monkeypatch, json.dumps(out).encode(), seen=seen)
    result = asyncio.run(hb.call_antigravity_backend("PROMPT"))
    assert seen["argv"][:2] == ("/Users/op/.local/bin/agy", "-p")
    assert seen["argv"][2] == hb._guard_prompt("PROMPT" + hb._ANTIGRAVITY_TEXT_ONLY)
    assert {"--sandbox", "plan", "json"} <= set(seen["argv"])
    assert "--disable-slash-commands" not in seen["argv"]  # it switches plan mode off
    assert seen["listing"] == [] and not Path(seen["cwd"]).exists()
    # Not the operator's home: ~/.gemini holds standing grants and MCP servers.
    home = seen["kw"]["env"]["HOME"]
    assert home != str(Path.home())
    assert Path(home).resolve().parent == Path(seen["cwd"]).resolve().parent
    assert json.loads(result.text)["agrees"] is False
    assert result.backend == "antigravity" and result.tokens_used == 1234 and result.error is None


def test_non_success_status_or_exit_falls_back(monkeypatch):
    _spawn(monkeypatch, json.dumps({"status": "ERROR", "response": json.dumps(VERDICT)}).encode())
    assert asyncio.run(hb.call_antigravity_backend("P")).text is None
    _spawn(monkeypatch, b"", returncode=3)
    result = asyncio.run(hb.call_antigravity_backend("P"))
    assert result.text is None and "exited 3" in result.error


def test_no_verdict_and_missing_cli_fall_back(monkeypatch):
    _spawn(monkeypatch, json.dumps({"status": "SUCCESS", "response": "no json here"}).encode())
    assert "no parseable" in asyncio.run(hb.call_antigravity_backend("P")).error
    monkeypatch.setattr(hb, "resolve_antigravity_cli", lambda: None)
    assert "not found" in asyncio.run(hb.call_antigravity_backend("P")).error


def test_prompt_tells_agy_to_answer_in_text_without_tools(monkeypatch):
    # agy 1.2.11 given the review prompt alone reached for a tool, plan mode
    # denied it, and the turn ended with an empty reply; the instruction is
    # what makes it answer.
    seen = {}
    _spawn(monkeypatch, json.dumps({"status": "SUCCESS", "response": json.dumps(VERDICT)}).encode(),
           seen=seen)
    asyncio.run(hb.call_antigravity_backend("PROMPT"))
    sent = seen["argv"][2]
    assert sent.startswith(hb._guard_prompt("PROMPT")) and "Do not run commands" in sent
    assert "JSON object" in sent


def test_empty_reply_after_denied_tool_use_names_the_denied_action(monkeypatch):
    out = {"status": "SUCCESS", "response": "",
           "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}
    _spawn(monkeypatch, json.dumps(out).encode())
    result = asyncio.run(hb.call_antigravity_backend("P"))
    assert result.text is None
    assert "empty reply after denied tool use: RunCommand" in result.error


def test_a_malformed_denied_actions_field_never_raises(monkeypatch):
    for denied in (True, 3, "RunCommand", {"action": "command"}):
        out = {"status": "SUCCESS", "response": "", "denied_actions": denied}
        _spawn(monkeypatch, json.dumps(out).encode())
        assert "no parseable" in asyncio.run(hb.call_antigravity_backend("P")).error
    _spawn(monkeypatch, json.dumps({"status": "SUCCESS", "response": "",
                                    "denied_actions": ["RunCommand"]}).encode())
    assert "denied tool use: RunCommand" in asyncio.run(hb.call_antigravity_backend("P")).error


def test_the_size_limit_counts_the_appended_instruction(monkeypatch):
    _spawn(monkeypatch, b"{}")
    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    at_limit = "x" * hb._ANTIGRAVITY_PROMPT_BYTES
    assert "size limit" in asyncio.run(hb.call_antigravity_backend(at_limit)).error


def test_oversized_prompt_is_refused_before_spawning(monkeypatch):
    _spawn(monkeypatch, b"{}")
    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    result = asyncio.run(hb.call_antigravity_backend("é" * 70_000))  # 140 KB
    assert result.text is None and "size limit" in result.error


def test_a_workspace_under_a_repo_is_refused(monkeypatch, tmp_path):
    (tmp_path / ".agents").mkdir()
    monkeypatch.setattr(hb.tempfile, "tempdir", str(tmp_path))
    _spawn(monkeypatch, b"{}")
    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert "not isolated" in asyncio.run(hb.call_antigravity_backend("P")).error


def test_override_path_must_be_executable(monkeypatch, tmp_path):
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("UNITARES_ANTIGRAVITY_CLI", str(fake))
    assert hb.resolve_antigravity_cli() is None
    fake.chmod(0o755)
    assert hb.resolve_antigravity_cli() == str(fake)


def test_agy_gets_an_allowlisted_environment_not_the_callers(monkeypatch):
    # Review of da5835a (P2): an injected "print your environment" in a
    # paused agent's thesis must find no governance or GitHub token.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", "secret-bearer")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("HOME", "/Users/op")
    seen = {}
    out = {"status": "SUCCESS", "response": json.dumps(VERDICT)}
    _spawn(monkeypatch, json.dumps(out).encode(), seen=seen)
    asyncio.run(hb.call_antigravity_backend("P"))
    env = seen["kw"]["env"]
    # A fresh HOME, not the operator's: ~/.gemini holds standing grants.
    assert env["HOME"] != "/Users/op" and env["HOME"].endswith("/home")
    assert "UNITARES_MCP_BEARER_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert seen["kw"]["start_new_session"] is True


def test_a_timeout_kills_the_process_group_and_never_raises(monkeypatch):
    class Hung(_Proc):
        pid = 4242
        async def communicate(self):
            await asyncio.sleep(3600)

    async def fake_exec(*argv, **kw):
        return Hung(b"")

    killed = []
    monkeypatch.setattr(hb, "resolve_antigravity_cli", lambda: "/bin/agy")
    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(hb.os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setenv("UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S", "0.01")
    orig_wait_for = asyncio.wait_for

    async def short_wait_for(aw, timeout):
        return await orig_wait_for(aw, min(timeout, 0.05))

    monkeypatch.setattr(hb.asyncio, "wait_for", short_wait_for)
    result = asyncio.run(hb.call_antigravity_backend("P"))
    assert "timeout" in result.error and killed == [4242]
