"""
Cold-start metadata load: regression tests for the v0.2 RESOLUTION fix.

Pre-fix `_load_metadata_from_postgres_async` had two per-agent await loops
(`metadata_cache.set` and `is_lineage_provisional` via `resolve_trust_tier`)
that produced a ~17s first-call tax on observe with ~3000 agents. The fix
batches the provisional check, makes per-agent trust-tier resolution sync
via `prefetched_provisional`, and defers redis cache hydration until after
`_metadata_loaded=True` is flipped. These tests pin those invariants.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _fake_agent_row(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        status="active",
        created_at=None,
        last_activity_at=None,
        updated_at=None,
        tags=[],
        notes="",
        purpose=None,
        parent_agent_id=None,
        spawn_reason=None,
        health_status="unknown",
        metadata={"api_key": "k", "agent_uuid": agent_id},
    )


def _fake_identity(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        metadata={"trajectory_current": {"E": 0.5}, "tags": []},
    )


@pytest.mark.asyncio
async def test_no_per_agent_provisional_fetch():
    """is_lineage_provisional must NOT be called per agent during cold load.

    Pre-fix this fired N times (N = agent count) — the dominant cold-start
    cost. Post-fix the bulk get_provisional_lineage_set replaces it.
    """
    from src import agent_metadata_persistence
    from src import agent_metadata_model

    agent_ids = [f"agent-{i:04d}" for i in range(50)]
    agents = [_fake_agent_row(aid) for aid in agent_ids]
    identities = {aid: _fake_identity(aid) for aid in agent_ids}

    fake_db = MagicMock()
    fake_db.get_identities_batch = AsyncMock(return_value=identities)
    fake_db.get_provisional_lineage_set = AsyncMock(return_value=set())
    fake_db.is_lineage_provisional = AsyncMock(return_value=False)

    with patch("src.agent_storage.list_agents", new=AsyncMock(return_value=agents)), \
         patch("src.db.get_db", return_value=fake_db):
        agent_metadata_model._metadata_loaded = False
        agent_metadata_model.agent_metadata.clear()
        await agent_metadata_persistence.load_metadata_async()

    assert fake_db.get_provisional_lineage_set.await_count == 1, \
        "expected exactly one batch provisional read"
    assert fake_db.is_lineage_provisional.await_count == 0, \
        "per-agent is_lineage_provisional must not fire on cold load"


@pytest.mark.asyncio
async def test_loaded_flag_flips_before_cache_hydration():
    """`_metadata_loaded=True` must be set before metadata_cache.set runs.

    The ordering is what makes observe handlers cold-start fast: they
    `await load_metadata_async()`, hit the fast-path, and return without
    waiting on per-agent redis writes.
    """
    from src import agent_metadata_persistence
    from src import agent_metadata_model

    agent_ids = [f"agent-{i:04d}" for i in range(20)]
    agents = [_fake_agent_row(aid) for aid in agent_ids]
    identities = {aid: _fake_identity(aid) for aid in agent_ids}

    loaded_when_first_set: dict[str, Optional[bool]] = {"value": None}

    fake_cache = MagicMock()

    async def _tracking_set(agent_id: str, value: Any, ttl: int = 300) -> None:
        if loaded_when_first_set["value"] is None:
            loaded_when_first_set["value"] = bool(agent_metadata_model._metadata_loaded)

    fake_cache.set = AsyncMock(side_effect=_tracking_set)

    fake_db = MagicMock()
    fake_db.get_identities_batch = AsyncMock(return_value=identities)
    fake_db.get_provisional_lineage_set = AsyncMock(return_value=set())

    with patch("src.agent_storage.list_agents", new=AsyncMock(return_value=agents)), \
         patch("src.db.get_db", return_value=fake_db), \
         patch("src.cache.get_metadata_cache", return_value=fake_cache):
        agent_metadata_model._metadata_loaded = False
        agent_metadata_model.agent_metadata.clear()
        await agent_metadata_persistence.load_metadata_async()
        # Let the fire-and-forget hydration task run.
        for _ in range(3):
            await asyncio.sleep(0)

    assert loaded_when_first_set["value"] is True, \
        "_metadata_loaded must be True before any metadata_cache.set call"
    assert fake_cache.set.await_count == len(agent_ids), \
        "all agents must still get cache hydration eventually"


@pytest.mark.asyncio
async def test_provisional_lineage_propagates_to_trust_tier():
    """Agents in the provisional set must get the provisional-gate tier.

    The batch provisional set is the input; resolve_trust_tier's
    prefetched_provisional path is what consumes it. If the wiring breaks
    the provisional gate silently fails open, which is the bug R1 v3.3-D
    was added to prevent.
    """
    from src import agent_metadata_persistence
    from src import agent_metadata_model

    agent_ids = ["prov-agent", "regular-agent"]
    agents = [_fake_agent_row(aid) for aid in agent_ids]
    identities = {aid: _fake_identity(aid) for aid in agent_ids}

    fake_db = MagicMock()
    fake_db.get_identities_batch = AsyncMock(return_value=identities)
    fake_db.get_provisional_lineage_set = AsyncMock(return_value={"prov-agent"})
    fake_db.is_lineage_provisional = AsyncMock(return_value=False)

    with patch("src.agent_storage.list_agents", new=AsyncMock(return_value=agents)), \
         patch("src.db.get_db", return_value=fake_db):
        agent_metadata_model._metadata_loaded = False
        agent_metadata_model.agent_metadata.clear()
        await agent_metadata_persistence.load_metadata_async()

    prov = agent_metadata_model.agent_metadata.get("prov-agent")
    regular = agent_metadata_model.agent_metadata.get("regular-agent")
    assert prov is not None and regular is not None
    assert prov.trust_tier_num == 1, \
        "provisional agent must land at tier=1 via the gate"
    # The regular agent's tier depends on compute_trust_tier of its
    # metadata, but it must not be the provisional gate value with the
    # provisional source — the only invariant we assert here is that the
    # provisional gate did not apply to a non-provisional agent.
    assert regular.trust_tier != "provisional_lineage_gate"
