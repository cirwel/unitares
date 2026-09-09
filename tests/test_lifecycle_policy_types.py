"""`PERMANENT_TYPES` is the "never auto-archive" list, and it silently did not
cover the thing it was most obviously for.

It read `"architecture_decision"`. The storable type is
`"architectural_decision"`. Measured 2026-09-08 against the live KG, across
1,784 discoveries:

    architecture_decision   0 rows   (as coded — matched nothing, ever)
    architectural_decision 35 rows   (the real type, unprotected)
    root_cause_analysis     0 rows   (not a storable type)
    migration               0 rows   (not a storable type)
    learning               16 rows
    pattern                33 rows

Three of five entries were inert, so the protection covered 49 of 1,784 rows.
Not latent either: six architectural decisions — recorded operator decisions
and RFCs among them — were archived in one batch on 2026-06-27 with no closure
evidence, which is precisely what this set exists to prevent.

A membership test is the cheap guard. A policy set that names a value the
schema cannot hold is not a policy, it is a no-op that reads like one.
"""
from __future__ import annotations

import pytest

from src.knowledge_graph_lifecycle import (
    EPHEMERAL_TAGS,
    PERMANENT_TAGS,
    PERMANENT_TYPES,
)
from src.mcp_handlers.schemas.knowledge import DiscoveryType

try:  # pragma: no cover - depends on import layout
    from typing import get_args
except ImportError:  # pragma: no cover
    from typing_extensions import get_args  # type: ignore

STORABLE_TYPES = set(get_args(DiscoveryType))


def test_permanent_types_are_storable():
    """Every entry must be a value a discovery can actually carry."""
    unknown = PERMANENT_TYPES - STORABLE_TYPES
    assert not unknown, (
        f"PERMANENT_TYPES names {sorted(unknown)}, which DiscoveryType cannot "
        f"hold — those entries can never match a row, so the 'never "
        f"auto-archive' protection they claim to give does not exist. "
        f"Storable types: {sorted(STORABLE_TYPES)}"
    )


def test_architectural_decisions_are_protected():
    """The specific regression. An architectural decision is permanent by
    definition; the typo left all 35 of them archivable."""
    assert "architectural_decision" in PERMANENT_TYPES
    assert "architecture_decision" not in PERMANENT_TYPES, (
        "the misspelling is back — it matches no row and silently disables "
        "the protection for every architectural decision in the graph"
    )


@pytest.mark.parametrize("type_name", sorted(PERMANENT_TYPES))
def test_each_permanent_type_is_individually_valid(type_name):
    """Parameterised so a future addition names itself in the failure."""
    assert type_name in STORABLE_TYPES


def test_permanent_policy_actually_fires_for_an_architectural_decision():
    """Behaviour, not just membership: the classifier must return 'permanent'
    for an aged architectural decision rather than letting it age out."""
    from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle

    class _D:
        type = "architectural_decision"
        tags: list = []

    assert KnowledgeGraphLifecycle().get_lifecycle_policy(_D()) == "permanent"


def test_tag_policies_are_non_empty_and_disjoint():
    """Tags are free-form so they cannot drift from a schema, but a permanent
    tag that is also ephemeral would make the policy order load-bearing and
    invisible."""
    assert PERMANENT_TAGS and EPHEMERAL_TAGS
    assert not (PERMANENT_TAGS & EPHEMERAL_TAGS)
