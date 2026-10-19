"""The dialectic reviewer's codex backend runs under the lane overrides.

Codex's legacy ``--sandbox read-only`` reads the whole disk, and the review
prompt carries thesis text the paused agent wrote, so a reviewer launched that
way could be asked to read the operator's credentials and return them.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agents.dialectic_reviewer import reviewer
from src.mcp_handlers.support.codex_app_server_client import lane_config_overrides


@pytest.fixture(autouse=True)
def _confinement_verified(monkeypatch, request):
    monkeypatch.setattr(reviewer, "codex_confinement_verified", lambda cli: True)
    # Tests that spawn must not depend on the host having no administrator
    # codex configuration; the refusal tests set their own.
    if "admin" not in request.node.name:
        monkeypatch.setattr(reviewer, "codex_admin_config_conflict", lambda: None)


class _Proc:
    returncode = 0

    async def communicate(self):
        return b'{"verdict": "ok"}', b""

    def kill(self):
        pass


def test_reviewer_codex_call_uses_the_lane_overrides(monkeypatch):
    seen = {}

    async def fake_exec(*argv, env=None, cwd=None, **kwargs):
        seen["argv"] = argv
        seen["env"] = env
        seen["cwd"] = cwd
        seen["cwd_existed"] = os.path.isdir(cwd)
        return _Proc()

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(
        reviewer, "codex_mcp_disable_overrides", lambda cli, base, cwd: ["mcp_servers.helper.enabled=false"]
    )
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(reviewer.call_codex_reviewer("review this"))

    script = seen["argv"][2]
    assert "--sandbox" not in script
    profile = seen["env"]["DR_CODEX_C1"].split('"')[1]
    expected = lane_config_overrides("/opt/x/codex", profile) + ["mcp_servers.helper.enabled=false"]
    for index, value in enumerate(expected, start=1):
        assert f'-c "$DR_CODEX_C{index}"' in script
        assert seen["env"][f"DR_CODEX_C{index}"] == value
    assert seen["env"]["DR_PROMPT"] == "review this"
    # An empty private directory, removed afterwards.
    assert seen["cwd_existed"] and not os.path.exists(seen["cwd"])


def test_reviewer_codex_call_refuses_a_config_it_cannot_neutralise(monkeypatch):
    def refuse(cli, base, cwd):
        raise ValueError("codex MCP servers still enabled: stubborn")

    async def fake_exec(*argv, **kwargs):
        raise AssertionError("codex must not be launched")

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer, "codex_mcp_disable_overrides", refuse)
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(reviewer.call_codex_reviewer("review this")) is None


def test_reviewer_codex_call_refuses_under_administrator_config(monkeypatch):
    async def fake_exec(*argv, **kwargs):
        raise AssertionError("codex must not be launched")

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer, "codex_admin_config_conflict", lambda: "/etc/codex/config.toml")
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(reviewer.call_codex_reviewer("review this")) is None


def test_reviewer_codex_call_refuses_an_unconfining_cli(monkeypatch):
    async def fake_exec(*argv, **kwargs):
        raise AssertionError("codex must not be launched")

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer, "codex_confinement_verified", lambda cli: False)
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(reviewer.call_codex_reviewer("review this")) is None


def test_reviewer_codex_call_gets_no_secret_and_its_own_process_group(monkeypatch):
    """The reviewer runs under the orchestrator, whose environment holds
    bearers and metered API keys (OPENAI_API_KEY would move codex onto
    metered billing). The CLI gets an allowlist, and a timeout kills its
    whole process group, sandboxed children included."""
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_ORCHESTRATOR_BEARER_TOKEN",
                 "DIALECTIC_TEST_CANARY"):
        monkeypatch.setenv(name, "secret-value")
    seen = {}

    async def fake_exec(*argv, env=None, cwd=None, **kwargs):
        seen.update(env=env, kwargs=kwargs)
        return _Proc()

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer, "codex_mcp_disable_overrides", lambda cli, base, cwd: [])
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(reviewer.call_codex_reviewer("review this"))
    assert "secret-value" not in seen["env"].values()
    assert seen["env"]["HOME"] == os.environ["HOME"]
    assert seen["kwargs"]["start_new_session"] is True


def test_reviewer_codex_timeout_kills_the_process_group(monkeypatch):
    groups = []

    class _Slow(_Proc):
        pid = 5151

        async def communicate(self):
            # Until its group is killed, as a hung CLI would; then it exits.
            while 5151 not in groups:
                await asyncio.sleep(0.01)
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        return _Slow()

    monkeypatch.setenv("UNITARES_DIALECTIC_CODEX_TIMEOUT_S", "0.05")
    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer, "codex_mcp_disable_overrides", lambda cli, base, cwd: [])
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    from agents.dialectic_reviewer import host_backends

    monkeypatch.setattr(host_backends.os, "killpg", lambda pid, sig: groups.append(pid))
    assert asyncio.run(reviewer.call_codex_reviewer("review this")) is None
    assert groups == [5151]


def test_the_cli_allowlist_matches_the_review_gate_reviewer_allowlist():
    """Both run the same subscription CLIs; the lists must not drift."""
    import importlib.util
    import sys
    from pathlib import Path

    from agents.dialectic_reviewer import host_backends

    path = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("_rg_allowlist_parity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert set(host_backends._CLI_ENV_ALLOWLIST) == set(module.REVIEWER_ENV_ALLOWLIST)
