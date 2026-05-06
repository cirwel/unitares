from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.identity.provenance_chain import build_lineage_provenance_chain


NOW = datetime(2026, 5, 6, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, rows, identities):
        self.rows = rows
        self.identities = identities
        self.reads = []

    async def read_lineage_state(self, successor_id):
        self.reads.append(successor_id)
        return self.rows.get(successor_id)

    async def get_identity(self, agent_id):
        present = self.identities.get(agent_id)
        if present:
            return SimpleNamespace(agent_id=agent_id)
        return None


def _row(parent_id, *, provisional=False, confirmed=True, chain_obs_count=0):
    return {
        "parent_agent_id": parent_id,
        "provisional_lineage": provisional,
        "confirmed_at": NOW if confirmed else None,
        "lineage_declared_at": NOW,
        "lineage_demoted_at": None,
        "lineage_archived_at": None,
        "lineage_last_eval_at": NOW,
        "chain_obs_count": chain_obs_count,
    }


@pytest.mark.asyncio
async def test_build_lineage_provenance_chain_orders_root_to_writer():
    db = FakeDb(
        rows={
            "child": _row("mid", chain_obs_count=2),
            "mid": _row("root", chain_obs_count=5),
            "root": {
                "parent_agent_id": None,
                "provisional_lineage": False,
                "confirmed_at": None,
                "lineage_declared_at": None,
                "lineage_demoted_at": None,
                "lineage_archived_at": None,
                "lineage_last_eval_at": None,
                "chain_obs_count": 0,
            },
        },
        identities={"mid": True, "root": True},
    )

    chain = await build_lineage_provenance_chain("child", db=db)

    assert [link["parent_agent_id"] for link in chain] == ["root", "mid"]
    assert [link["successor_agent_id"] for link in chain] == ["mid", "child"]
    assert [link["depth_from_writer"] for link in chain] == [2, 1]
    assert all(link["schema"] == "s7.lineage_link.v1" for link in chain)
    assert all(link["lineage_state"] == "confirmed" for link in chain)
    assert all(link["aggregation_eligible_at_write"] is True for link in chain)
    assert chain[0]["confirmed_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_build_lineage_provenance_chain_flags_missing_parent_and_stops():
    db = FakeDb(
        rows={"child": _row("missing-parent")},
        identities={"missing-parent": False},
    )

    chain = await build_lineage_provenance_chain("child", db=db)

    assert len(chain) == 1
    assert chain[0]["parent_agent_id"] == "missing-parent"
    assert chain[0]["successor_agent_id"] == "child"
    assert chain[0]["chain_stop_reason"] == "parent_identity_missing"


@pytest.mark.asyncio
async def test_build_lineage_provenance_chain_marks_provisional_ineligible():
    db = FakeDb(
        rows={
            "child": _row("parent", provisional=True, confirmed=False),
            "parent": {"parent_agent_id": None},
        },
        identities={"parent": True},
    )

    chain = await build_lineage_provenance_chain("child", db=db)

    assert chain[0]["lineage_state"] == "provisional"
    assert chain[0]["provisional_lineage"] is True
    assert chain[0]["aggregation_eligible_at_write"] is False


@pytest.mark.asyncio
async def test_build_lineage_provenance_chain_detects_cycle():
    db = FakeDb(
        rows={
            "child": _row("parent"),
            "parent": _row("child"),
        },
        identities={"parent": True, "child": True},
    )

    chain = await build_lineage_provenance_chain("child", db=db)

    assert chain[0]["chain_stop_reason"] == "cycle_detected"
    assert len(chain) == 2
