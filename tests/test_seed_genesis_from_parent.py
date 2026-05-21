"""Lineage-seeded genesis — S6/R3-Q2 primitive.

 R3 appendix Q2 + S6 options appendix.
Under ontology v2, session-like agents rarely accumulate the 50/200
observations `compute_trust_tier` expects. Seeding genesis from the
declared parent's `trajectory_current` gives the child a meaningful
lineage baseline.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src import trajectory_identity


def _identity(metadata):
    return SimpleNamespace(metadata=metadata)


@pytest.mark.asyncio
async def test_seeds_child_genesis_from_parent_current():
    """Parent has trajectory_current, child has no genesis — write parent's
    current into child's genesis and stamp provenance."""
    child_uuid = "c0000000-0000-0000-0000-000000000001"
    parent_uuid = "90000000-0000-0000-0000-000000000002"
    parent_current = {
        "observation_count": 87,
        "identity_confidence": 0.78,
        "attractor": [0.1, 0.2, 0.3],
    }

    child_meta = {}
    db = AsyncMock()
    db.get_identity = AsyncMock(side_effect=[
        _identity(child_meta),
        _identity({"trajectory_current": parent_current}),
    ])
    db.update_identity_metadata = AsyncMock(return_value=True)

    with patch("src.db.get_db", return_value=db):
        result = await trajectory_identity.seed_genesis_from_parent(child_uuid, parent_uuid)

    assert result["seeded"] is True
    assert result["source"] == "parent_lineage"
    assert result["parent_agent_id"] == parent_uuid
    # update_identity_metadata called with child's uuid and the seeded metadata
    db.update_identity_metadata.assert_called_once()
    call_args = db.update_identity_metadata.call_args
    assert call_args.args[0] == child_uuid
    seeded_meta = call_args.args[1]
    assert seeded_meta["trajectory_genesis"] == parent_current
    assert seeded_meta["trajectory_genesis_source"]["parent_agent_id"] == parent_uuid
    assert seeded_meta["trajectory_genesis_source"]["source"] == "parent_lineage"


@pytest.mark.asyncio
async def test_refuses_to_reseed_when_child_at_tier_2_or_above():
    """Child already has genesis at tier >= 2 — immutability holds."""
    child_uuid = "c0000000-0000-0000-0000-000000000001"
    parent_uuid = "90000000-0000-0000-0000-000000000002"
    child_meta = {
        "trajectory_genesis": {"observation_count": 60, "identity_confidence": 0.6},
        "trust_tier": {"tier": 2, "name": "established"},
    }

    db = AsyncMock()
    db.get_identity = AsyncMock(return_value=_identity(child_meta))
    db.update_identity_metadata = AsyncMock()

    with patch("src.db.get_db", return_value=db):
        result = await trajectory_identity.seed_genesis_from_parent(child_uuid, parent_uuid)

    assert result["seeded"] is False
    assert "immutable" in result["reason"]
    db.update_identity_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_parent_has_no_trajectory_current():
    """Parent has metadata but no trajectory_current — nothing to seed from."""
    child_uuid = "c0000000-0000-0000-0000-000000000001"
    parent_uuid = "90000000-0000-0000-0000-000000000002"

    db = AsyncMock()
    db.get_identity = AsyncMock(side_effect=[
        _identity({}),
        _identity({"trajectory_genesis": {"observation_count": 1}}),  # no current
    ])
    db.update_identity_metadata = AsyncMock()

    with patch("src.db.get_db", return_value=db):
        result = await trajectory_identity.seed_genesis_from_parent(child_uuid, parent_uuid)

    assert result["seeded"] is False
    assert "no trajectory_current" in result["reason"]
    db.update_identity_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_parent_not_found():
    """Parent identity missing — no-op."""
    child_uuid = "c0000000-0000-0000-0000-000000000001"
    parent_uuid = "90000000-0000-0000-0000-000000000002"

    db = AsyncMock()
    db.get_identity = AsyncMock(side_effect=[
        _identity({}),
        None,
    ])
    db.update_identity_metadata = AsyncMock()

    with patch("src.db.get_db", return_value=db):
        result = await trajectory_identity.seed_genesis_from_parent(child_uuid, parent_uuid)

    assert result["seeded"] is False
    assert "parent" in result["reason"].lower()
    db.update_identity_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_reseeds_when_child_at_tier_1_with_stale_genesis():
    """Child has a genesis but tier < 2 — reseed from parent is allowed
    (matches the partial reseed policy in store_genesis_signature)."""
    child_uuid = "c0000000-0000-0000-0000-000000000001"
    parent_uuid = "90000000-0000-0000-0000-000000000002"
    child_meta = {
        "trajectory_genesis": {"observation_count": 3, "identity_confidence": 0.15},
        "trust_tier": {"tier": 1, "name": "emerging"},
    }
    parent_current = {"observation_count": 120, "identity_confidence": 0.82}

    db = AsyncMock()
    db.get_identity = AsyncMock(side_effect=[
        _identity(child_meta),
        _identity({"trajectory_current": parent_current}),
    ])
    db.update_identity_metadata = AsyncMock(return_value=True)

    with patch("src.db.get_db", return_value=db):
        result = await trajectory_identity.seed_genesis_from_parent(child_uuid, parent_uuid)

    assert result["seeded"] is True
    call_args = db.update_identity_metadata.call_args
    assert call_args.args[1]["trajectory_genesis"] == parent_current
