from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.identity.provenance_chain import build_lineage_provenance_chain
from src.identity.provenance_chain import evaluate_lineage_chain_aggregation
from src.identity.provenance_chain import aggregate_lineage_attribution


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


def _link(parent_id, successor_id, *, state="confirmed", eligible=True):
    return {
        "schema": "s7.lineage_link.v1",
        "source": "core.identities",
        "parent_agent_id": parent_id,
        "successor_agent_id": successor_id,
        "relationship": "lineage_parent",
        "lineage_state": state,
        "provisional_lineage": state == "provisional",
        "aggregation_eligible_at_write": eligible,
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


@pytest.mark.asyncio
async def test_as_written_aggregation_uses_snapshot_without_db_reads():
    db = FakeDb(rows={}, identities={})
    chain = [_link("root", "child")]

    result = await evaluate_lineage_chain_aggregation(
        chain,
        mode="as_written",
        db=db,
    )

    assert result["eligible"] is True
    assert result["root_agent_id"] == "root"
    assert result["direct_parent_agent_id"] == "root"
    assert result["writer_agent_id"] == "child"
    assert result["lineage_agent_ids"] == ["root"]
    assert db.reads == []


@pytest.mark.asyncio
async def test_as_written_excludes_provisional_by_default_with_opt_in():
    chain = [_link("parent", "child", state="provisional", eligible=False)]

    default_result = await evaluate_lineage_chain_aggregation(
        chain,
        mode="as_written",
    )
    provisional_result = await evaluate_lineage_chain_aggregation(
        chain,
        mode="as_written",
        include_provisional=True,
    )

    assert default_result["eligible"] is False
    assert default_result["reason"] == "link_not_eligible_at_write"
    assert provisional_result["eligible"] is True
    assert provisional_result["provisional_included"] is True


@pytest.mark.asyncio
async def test_current_valid_rechecks_live_identity_state():
    db = FakeDb(
        rows={"child": _row("parent", confirmed=True)},
        identities={},
    )
    chain = [_link("parent", "child")]

    result = await evaluate_lineage_chain_aggregation(
        chain,
        mode="current_valid",
        db=db,
    )

    assert result["eligible"] is True
    assert db.reads == ["child"]


@pytest.mark.asyncio
async def test_current_valid_claws_back_demoted_or_reparented_link():
    chain = [_link("parent", "child")]
    demoted_db = FakeDb(
        rows={
            "child": {
                **_row("parent", confirmed=False),
                "lineage_demoted_at": NOW,
            }
        },
        identities={},
    )
    moved_db = FakeDb(
        rows={"child": _row("other-parent", confirmed=True)},
        identities={},
    )

    demoted = await evaluate_lineage_chain_aggregation(
        chain,
        mode="current_valid",
        db=demoted_db,
    )
    moved = await evaluate_lineage_chain_aggregation(
        chain,
        mode="current_valid",
        db=moved_db,
    )

    assert demoted["eligible"] is False
    assert demoted["reason"] == "current_invalid"
    assert demoted["current_failures"][0]["current_lineage_state"] == "demoted"
    assert moved["eligible"] is False
    assert moved["current_failures"][0]["reason"] == "current_parent_mismatch"


@pytest.mark.asyncio
async def test_current_valid_requires_write_time_eligibility():
    db = FakeDb(
        rows={"child": _row("parent", confirmed=True)},
        identities={},
    )
    chain = [_link("parent", "child", state="provisional", eligible=False)]

    result = await evaluate_lineage_chain_aggregation(
        chain,
        mode="current_valid",
        db=db,
    )

    assert result["eligible"] is False
    assert result["reason"] == "link_not_eligible_at_write"
    assert db.reads == []


@pytest.mark.asyncio
async def test_aggregate_lineage_attribution_counts_eligible_chains():
    db = FakeDb(
        rows={
            "parent-a": _row("root", confirmed=True),
            "child-a": _row("parent-a", confirmed=True),
            "child-b": _row("parent-b", confirmed=True),
        },
        identities={},
    )
    discoveries = [
        SimpleNamespace(
            agent_id="child-a",
            provenance_chain=[
                _link("root", "parent-a"),
                _link("parent-a", "child-a"),
            ],
        ),
        SimpleNamespace(
            agent_id="child-b",
            provenance_chain=[
                _link(
                    "parent-b",
                    "child-b",
                    state="provisional",
                    eligible=False,
                )
            ],
        ),
        SimpleNamespace(agent_id="legacy", provenance_chain=None),
    ]

    result = await aggregate_lineage_attribution(
        discoveries,
        mode="current_valid",
        db=db,
    )

    assert result["total_discoveries"] == 3
    assert result["eligible_discoveries"] == 1
    assert result["excluded_discoveries"] == 2
    assert result["by_root_agent_id"] == {"root": 1}
    assert result["by_direct_parent_agent_id"] == {"parent-a": 1}
    assert result["by_lineage_agent_id"] == {"root": 1, "parent-a": 1}
    assert result["by_writer_agent_id"] == {"child-a": 1}
    assert result["excluded_reasons"] == {
        "link_not_eligible_at_write": 1,
        "missing_provenance_chain": 1,
    }
