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

What this has already cost is NOT established, and the first draft of this file
claimed it was. 14 architectural decisions were archived in one minute on
2026-06-27 with no protective tag, but a batch that is 100% a single type reads
as a deliberate type-targeted sweep rather than an age-based archiver, which
would have caught mixed types. The claim is withdrawn: an inert policy set is
the defect on its own terms.

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
from typing import get_args

from src.mcp_handlers.knowledge.handlers import VALID_DISCOVERY_TYPES
from src.mcp_handlers.schemas.knowledge import DiscoveryType

# The authority is what can be STORED, not what can be typed. Two
# hand-maintained vocabularies exist and they disagree: the schema Literal
# carries the alias `bug`, the handler set carries `topic_rollup`. Review,
# 2026-09-08, showed both weaker choices fail:
#
#   - pinning to the Literal alone accepts `bug`, which handlers normalize to
#     `bug_found` before storage, so no row can ever carry it — the exact
#     inert-membership failure this guard exists to catch, re-committed
#     through the guard itself;
#   - pinning to the union accepts it for the same reason.
#
# So: canonical stored values only. Alias spellings are an input concern.
SCHEMA_TYPES = set(get_args(DiscoveryType))
HANDLER_TYPES = set(VALID_DISCOVERY_TYPES)
KNOWN_TYPES = HANDLER_TYPES

# Types no MCP caller can write, but the storage layer can: DiscoveryNode.type
# is unconstrained and the column is plain TEXT. Named here so the guard does
# not force their removal, and so the exemption is explicit rather than a
# silent hole.
BACKEND_ONLY_TYPES = {"root_cause_analysis", "migration"}


def test_permanent_types_are_known_or_explicitly_backend_only():
    """Every entry is a name something can actually write.

    The failure this pins: PERMANENT_TYPES read `architecture_decision`, which
    neither vocabulary contains and no row has ever carried.
    """
    unknown = PERMANENT_TYPES - KNOWN_TYPES - BACKEND_ONLY_TYPES
    assert not unknown, (
        f"PERMANENT_TYPES names {sorted(unknown)}, which nothing can write — "
        f"those entries can never match a row, so the 'never auto-archive' "
        f"protection they claim to give does not exist. Known types: "
        f"{sorted(KNOWN_TYPES)}; backend-only exemptions: "
        f"{sorted(BACKEND_ONLY_TYPES)}"
    )


def test_an_alias_spelling_cannot_satisfy_the_guard():
    """The concrete attack: `bug` is accepted input, normalized to `bug_found`
    before storage, so it can never be a stored value. A guard that accepts it
    would let PERMANENT_TYPES claim a protection covering zero rows."""
    assert "bug" in SCHEMA_TYPES, "premise changed — re-derive this test"
    assert "bug" not in KNOWN_TYPES, (
        "the guard's authority admits an alias spelling; PERMANENT_TYPES "
        "could name it and protect nothing"
    )


def test_the_two_type_vocabularies_are_reconciled():
    """They already drift, and a guard pinned to either alone is unsound.

    This does not demand equality — the divergence is deliberate — it demands
    that it stay the KNOWN divergence, so a new one has to be looked at.
    """
    assert SCHEMA_TYPES - HANDLER_TYPES == {"bug"}, (
        f"schema-only types changed: {sorted(SCHEMA_TYPES - HANDLER_TYPES)}"
    )
    assert HANDLER_TYPES - SCHEMA_TYPES == {"topic_rollup"}, (
        f"handler-only types changed: {sorted(HANDLER_TYPES - SCHEMA_TYPES)}"
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
    assert type_name in KNOWN_TYPES | BACKEND_ONLY_TYPES


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
