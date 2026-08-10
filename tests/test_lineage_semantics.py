"""Canonical spawn-reason semantics shared by lineage lifecycle consumers."""

import pytest

from src.identity.lineage_semantics import (
    DISPATCHED_CHILD_SPAWN_REASONS,
    INTENTIONAL_LINEAGE_SPAWN_REASONS,
    NON_SUCCESSION_SPAWN_REASONS,
    LineageSpawnReason,
    ParentRelationship,
    allows_live_parent,
    classify_parent_relationship,
    is_intentional_lineage_reason,
    is_non_succession_spawn_reason,
)


@pytest.mark.parametrize(
    ("reason", "relationship", "intentional"),
    [
        ("subagent", ParentRelationship.DISPATCHED_CHILD, True),
        ("dialectic_reviewer", ParentRelationship.DISPATCHED_CHILD, True),
        ("dispatch", ParentRelationship.DISPATCHED_CHILD, True),
        ("compaction", ParentRelationship.CONTEXT_CONTINUATION, True),
        ("explicit", ParentRelationship.SUCCESSION, True),
        ("new_session", ParentRelationship.SUCCESSION, False),
    ],
)
def test_registered_reason_carries_all_lifecycle_semantics(
    reason,
    relationship,
    intentional,
):
    assert classify_parent_relationship(reason) is relationship
    assert allows_live_parent(reason) is relationship.allows_live_parent
    assert is_non_succession_spawn_reason(reason) is relationship.allows_live_parent
    assert is_intentional_lineage_reason(reason) is intentional


@pytest.mark.parametrize("reason", [None, "", "dialectic_revewer", "fleet_dispatch"])
def test_unknown_reason_fails_closed(reason):
    assert classify_parent_relationship(reason) is ParentRelationship.UNKNOWN
    assert allows_live_parent(reason) is False
    assert is_non_succession_spawn_reason(reason) is False
    assert is_intentional_lineage_reason(reason) is False


def test_derived_reason_sets_cannot_drift_from_registry():
    expected_non_succession = tuple(
        reason.value
        for reason in LineageSpawnReason
        if reason.parent_relationship.allows_live_parent
    )
    expected_intentional = {
        reason.value for reason in LineageSpawnReason if reason.intentional_by_reason
    }
    expected_dispatched = {
        reason.value
        for reason in LineageSpawnReason
        if reason.parent_relationship is ParentRelationship.DISPATCHED_CHILD
    }

    assert NON_SUCCESSION_SPAWN_REASONS == expected_non_succession
    assert DISPATCHED_CHILD_SPAWN_REASONS == expected_dispatched
    assert INTENTIONAL_LINEAGE_SPAWN_REASONS == expected_intentional


def test_dialectic_reviewer_uses_registered_reason():
    from agents.dialectic_reviewer.reviewer import SPAWN_REASON

    assert SPAWN_REASON == LineageSpawnReason.DIALECTIC_REVIEWER.value
    assert allows_live_parent(SPAWN_REASON) is True
