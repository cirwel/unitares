"""Canonical semantics for lineage-bearing ``spawn_reason`` values.

``spawn_reason`` is descriptive metadata, not dispatch attestation.  It still
drives several lifecycle decisions, though: whether a live parent is expected,
whether a child supersedes its parent, and whether a reason by itself describes
an intentional causal edge.  Keeping those decisions in independent string
allowlists caused #1485: the dialectic reviewer was a dispatched child at its
producer, but succession-shaped everywhere else.

Every in-tree reason that claims a lifecycle exemption is registered here with
its parent relationship.  Unregistered harness labels and external values
remain supported for compatibility, but classify as ``unknown`` and therefore
do *not* receive the live-parent or non-succession exemptions.  A future
orchestrator-vouch can replace this declarative classification with verified
dispatch evidence without changing the lifecycle consumers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class ParentRelationship(StrEnum):
    """How a lineage child relates to its declared parent at creation time."""

    DISPATCHED_CHILD = "dispatched_child"
    CONTEXT_CONTINUATION = "context_continuation"
    SUCCESSION = "succession"
    UNKNOWN = "unknown"

    @property
    def allows_live_parent(self) -> bool:
        """Whether the relationship expects the parent to remain live."""

        return self in {
            ParentRelationship.DISPATCHED_CHILD,
            ParentRelationship.CONTEXT_CONTINUATION,
        }


class LineageSpawnReason(StrEnum):
    """Registered lineage reasons with their lifecycle semantics attached.

    Adding a specialized in-tree reason requires choosing its relationship in
    the same declaration.  That keeps a producer from silently creating a new
    string which downstream liveness and succession guards interpret
    differently.
    """

    parent_relationship: ParentRelationship
    intentional_by_reason: bool

    def __new__(
        cls,
        value: str,
        parent_relationship: ParentRelationship,
        intentional_by_reason: bool,
    ) -> "LineageSpawnReason":
        member = str.__new__(cls, value)
        member._value_ = value
        member.parent_relationship = parent_relationship
        member.intentional_by_reason = intentional_by_reason
        return member

    SUBAGENT = (
        "subagent",
        ParentRelationship.DISPATCHED_CHILD,
        True,
    )
    DIALECTIC_REVIEWER = (
        "dialectic_reviewer",
        ParentRelationship.DISPATCHED_CHILD,
        True,
    )
    DISPATCH = (
        "dispatch",
        ParentRelationship.DISPATCHED_CHILD,
        True,
    )
    COMPACTION = (
        "compaction",
        ParentRelationship.CONTEXT_CONTINUATION,
        True,
    )
    EXPLICIT = (
        "explicit",
        ParentRelationship.SUCCESSION,
        True,
    )
    NEW_SESSION = (
        "new_session",
        ParentRelationship.SUCCESSION,
        False,
    )


def classify_parent_relationship(
    spawn_reason: Optional[str],
) -> ParentRelationship:
    """Return the registered relationship, failing closed for unknown values."""

    try:
        return LineageSpawnReason(spawn_reason).parent_relationship
    except (TypeError, ValueError):
        return ParentRelationship.UNKNOWN


def allows_live_parent(spawn_reason: Optional[str]) -> bool:
    """Whether ``spawn_reason`` permits a currently-live declared parent."""

    return classify_parent_relationship(spawn_reason).allows_live_parent


def is_non_succession_spawn_reason(spawn_reason: Optional[str]) -> bool:
    """Whether the child must not be used to supersede/archive its parent."""

    return classify_parent_relationship(spawn_reason).allows_live_parent


def is_intentional_lineage_reason(spawn_reason: Optional[str]) -> bool:
    """Whether the reason alone declares an intentional causal edge.

    ``new_session`` stays false because legacy SessionStart flows frequently
    used it for coincidental co-location.  A dead-parent ``new_session`` edge
    may still become valid through the normal provisional/R1 path.
    """

    try:
        return LineageSpawnReason(spawn_reason).intentional_by_reason
    except (TypeError, ValueError):
        return False


NON_SUCCESSION_SPAWN_REASONS = tuple(
    reason.value
    for reason in LineageSpawnReason
    if reason.parent_relationship.allows_live_parent
)

DISPATCHED_CHILD_SPAWN_REASONS = frozenset(
    reason.value
    for reason in LineageSpawnReason
    if reason.parent_relationship is ParentRelationship.DISPATCHED_CHILD
)

INTENTIONAL_LINEAGE_SPAWN_REASONS = frozenset(
    reason.value for reason in LineageSpawnReason if reason.intentional_by_reason
)
