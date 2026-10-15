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
def _confinement_verified(monkeypatch):
    monkeypatch.setattr(reviewer, "codex_confinement_verified", lambda cli: True)


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
