"""Groundskeeper's per-change KG summary note is opt-in (VIGIL_GROUNDSKEEPER_NOTES=1).

The audit and lifecycle cleanup run either way; only the note is gated.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agents.vigil.agent as vigil


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep these tests out of the operator's real Vigil log: agents/vigil/tests/
    does not inherit the redirect in tests/conftest.py."""
    monkeypatch.setattr(vigil, "LOG_FILE", tmp_path / "vigil-test.log", raising=False)
    monkeypatch.setenv("VIGIL_LOG_FILE", str(tmp_path / "vigil-test.log"))


class _Client:
    def __init__(self):
        self.notes = []
        self.cleanups = 0

    async def audit_knowledge(self, scope, top_n):
        return SimpleNamespace(success=True, audit={
            "buckets": {"stale": 5, "candidate_for_archive": 2}, "top_stale": []})

    async def cleanup_knowledge(self, dry_run):
        self.cleanups += 1
        return SimpleNamespace(success=True, cleaned_total=1)

    async def leave_note(self, summary, tags):
        self.notes.append((summary, tags))


async def _run(monkeypatch, flag, prev_state=None):
    from agents.vigil.agent import VigilAgent

    if flag is None:
        monkeypatch.delenv("VIGIL_GROUNDSKEEPER_NOTES", raising=False)
    else:
        monkeypatch.setenv("VIGIL_GROUNDSKEEPER_NOTES", flag)
    client = _Client()
    with patch("agents.vigil.agent.subprocess.run",
               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        summary = await VigilAgent()._run_groundskeeper(client, prev_state)
    return client, summary


@pytest.mark.asyncio
async def test_note_is_off_by_default_but_cleanup_still_runs(monkeypatch):
    client, summary = await _run(monkeypatch, None)
    assert client.notes == []
    assert client.cleanups == 1
    assert summary["stale_found"] == 7 and summary["archived"] == 1
    assert summary["note_suppressed"] is True


@pytest.mark.asyncio
async def test_opt_in_writes_the_ephemeral_note(monkeypatch):
    client, _ = await _run(monkeypatch, "1")
    ((text, tags),) = client.notes
    assert text.startswith("Groundskeeper: 7 stale")
    assert "ephemeral" in tags


@pytest.mark.asyncio
async def test_opt_in_still_suppresses_an_unchanged_backlog(monkeypatch):
    prev = {"groundskeeper_stale": 7, "groundskeeper_eligible": 0}
    client, summary = await _run(monkeypatch, "1", prev)
    assert client.notes == []
    assert summary["note_suppressed"] is True
