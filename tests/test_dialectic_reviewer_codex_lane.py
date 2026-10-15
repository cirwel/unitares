"""The dialectic reviewer's codex backend runs under the lane overrides.

Codex's legacy ``--sandbox read-only`` reads the whole disk, and the review
prompt carries thesis text the paused agent wrote, so a reviewer launched that
way could be asked to read the operator's credentials and return them.
"""

from __future__ import annotations

import asyncio

from agents.dialectic_reviewer import reviewer
from src.mcp_handlers.support.codex_app_server_client import lane_config_overrides


class _Proc:
    returncode = 0

    async def communicate(self):
        return b'{"verdict": "ok"}', b""

    def kill(self):
        pass


def test_reviewer_codex_call_uses_the_lane_overrides(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text('[mcp_servers.helper]\ncommand = "x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    seen = {}

    async def fake_exec(*argv, env=None, **kwargs):
        seen["argv"] = argv
        seen["env"] = env
        return _Proc()

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(reviewer.call_codex_reviewer("review this"))

    script = seen["argv"][2]
    assert "--sandbox" not in script
    expected = lane_config_overrides("/opt/x/codex") + ["mcp_servers.helper.enabled=false"]
    for index, value in enumerate(expected, start=1):
        assert f'-c "$DR_CODEX_C{index}"' in script
        assert seen["env"][f"DR_CODEX_C{index}"] == value
    assert seen["env"]["DR_PROMPT"] == "review this"


def test_reviewer_codex_call_refuses_a_config_it_cannot_neutralise(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text("not = [valid")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    async def fake_exec(*argv, **kwargs):
        raise AssertionError("codex must not be launched")

    monkeypatch.setattr(reviewer, "resolve_host_cli", lambda host_id: "/opt/x/codex")
    monkeypatch.setattr(reviewer.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(reviewer.call_codex_reviewer("review this")) is None
