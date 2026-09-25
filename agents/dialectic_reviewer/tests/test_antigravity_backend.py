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
            seen["argv"], seen["cwd"] = argv, cwd
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
    assert seen["argv"][:3] == ("/Users/op/.local/bin/agy", "-p", "PROMPT")
    assert {"--sandbox", "plan", "json"} <= set(seen["argv"])
    assert seen["listing"] == [] and not Path(seen["cwd"]).exists()
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
