"""refuse_fresh_onboard guard: residents refuse silent fresh-onboard.

Added 2026-04-19 as Phase 3 of the anchor-resilience series. Exercises the
three identity-resolution states for a resident with the flag set:

1. No anchor + no UNITARES_FIRST_RUN -> IdentityBootstrapRefused
2. No anchor + UNITARES_FIRST_RUN=1  -> onboard runs (explicit bootstrap)
3. Anchor present                     -> UUID-direct resume (guard irrelevant)
"""
from unittest.mock import AsyncMock

import pytest

from unitares_sdk.agent import GovernanceAgent
from unitares_sdk.errors import IdentityBootstrapRefused


class _Dummy(GovernanceAgent):
    async def run_cycle(self, client):
        return None


@pytest.mark.asyncio
async def test_refuse_raises_when_no_anchor(tmp_path, monkeypatch):
    """No anchor file + refuse=True + no FIRST_RUN env => raises."""
    anchor = tmp_path / "watcher.json"
    a = _Dummy(
        name="Watcher",
        mcp_url="stdio:test",
        session_file=anchor,
        refuse_fresh_onboard=True,
    )
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    client = AsyncMock()
    with pytest.raises(IdentityBootstrapRefused):
        await a._ensure_identity(client)
    client.onboard.assert_not_called()


@pytest.mark.asyncio
async def test_refuse_allows_when_first_run_set(tmp_path, monkeypatch):
    """No anchor file + refuse=True + FIRST_RUN=1 => allows onboard."""
    anchor = tmp_path / "watcher.json"
    a = _Dummy(
        name="Watcher",
        mcp_url="stdio:test",
        session_file=anchor,
        refuse_fresh_onboard=True,
    )
    monkeypatch.setenv("UNITARES_FIRST_RUN", "1")
    client = AsyncMock()
    client.onboard = AsyncMock()
    client.agent_uuid = "abc123"
    client.client_session_id = "s"
    client.continuity_token = "t"
    await a._ensure_identity(client)
    client.onboard.assert_called_once()


@pytest.mark.asyncio
async def test_refuse_resumes_normally_when_anchor_present(tmp_path, monkeypatch):
    """Anchor present => UUID-direct resume, flag irrelevant."""
    anchor = tmp_path / "watcher.json"
    anchor.write_text('{"agent_uuid": "8a930180-9ef9-59c7-9267-fd7dd936dd58"}')
    a = _Dummy(
        name="Watcher",
        mcp_url="stdio:test",
        session_file=anchor,
        refuse_fresh_onboard=True,
    )
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    client = AsyncMock()
    client.identity = AsyncMock()
    client.agent_uuid = "8a930180-9ef9-59c7-9267-fd7dd936dd58"
    client.client_session_id = "s"
    client.continuity_token = "t"
    await a._ensure_identity(client)
    client.identity.assert_called_once()
    client.onboard.assert_not_called()


@pytest.mark.asyncio
async def test_default_flag_false_allows_onboard(tmp_path, monkeypatch):
    """Default refuse_fresh_onboard=False preserves legacy onboard-on-missing behavior."""
    anchor = tmp_path / "ephemeral.json"
    a = _Dummy(
        name="Ephemeral",
        mcp_url="stdio:test",
        session_file=anchor,
    )
    monkeypatch.delenv("UNITARES_FIRST_RUN", raising=False)
    assert a.refuse_fresh_onboard is False
    client = AsyncMock()
    client.onboard = AsyncMock()
    client.agent_uuid = "abc123"
    client.client_session_id = "s"
    client.continuity_token = "t"
    await a._ensure_identity(client)
    client.onboard.assert_called_once()
