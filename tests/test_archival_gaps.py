"""Follow-up fixes for the 2026-06-14 lineage false-archival incident.

Gap 2: core.agents.archived_at was never written (timestamp lived only in
core.identities.disabled_at + audit.events). archive_agent now passes
archived_at to update_agent_fields so core.agents is self-consistent.

Gap 1: an archived agent's gate-refusal recovery hint misrouted to
forward-lineage (mint a NEW identity), when the same live process can reclaim
the SAME identity via onboard(resume=true) (auto-unarchive). The hint now
routes to the reclaim path first.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Gap 2 — archive_agent writes archived_at to core.agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_agent_writes_archived_at_to_core_agents():
    from src import agent_storage

    db = MagicMock()
    db.update_identity_status = AsyncMock()
    db.update_agent_fields = AsyncMock()

    iso = "2026-06-14T20:27:32+00:00"
    with patch.object(agent_storage, "_ensure_db_ready", new=AsyncMock()), \
         patch.object(agent_storage, "get_db", return_value=db):
        ok = await agent_storage.archive_agent("agent-1", archived_at=iso)

    assert ok is True
    # the timestamp must reach core.agents, not just core.identities
    db.update_agent_fields.assert_awaited_once()
    kwargs = db.update_agent_fields.await_args.kwargs
    assert kwargs["status"] == "archived"
    assert kwargs["archived_at"] == datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# Gap 1 — archived gate-refusal routes to reclaim, not forward-lineage
# ---------------------------------------------------------------------------


def test_archived_recovery_hint_routes_to_reclaim():
    from src.mcp_handlers.support import agent_auth

    uuid = "1b4172bb-8d50-447c-8a68-51a75e20eb26"
    srv = SimpleNamespace(agent_metadata={uuid: SimpleNamespace(status="archived")})

    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=srv):
        result = agent_auth.check_agent_can_operate(uuid)

    assert result is not None
    payload = json.loads(result.text)
    action = payload["recovery"]["action"].lower()
    # reclaim path surfaced (the capability already exists via onboard resume)
    assert "resume=true" in action
    # and it is not ONLY the forward "mint a new identity" guidance
    assert "auto-unarchive" in action or "reclaim" in action
