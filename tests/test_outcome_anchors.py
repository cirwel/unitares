"""Stage 0 — exogenous anchor tiering (src/grounding/outcome_anchors.py).

Guards Invariant 4 (a signal derived from the loop cannot anchor the loop): the
self-referential ``server_observation`` source and unknown/NULL provenance must
never be treated as anchors, and soft self-attestation must require explicit
opt-in.
"""
import pytest

from src.grounding.outcome_anchors import (
    AnchorTier,
    tier_for_source,
    is_exogenous_anchor,
    is_anchorable,
    anchored_outcomes_predicate,
    ANCHORED_OUTCOMES_SQL,
    ANCHORED_OUTCOMES_WITH_SOFT_SQL,
    JOINABLE_SNAPSHOT_SQL,
)


@pytest.mark.parametrize("source,expected", [
    ("external_signal", AnchorTier.TRUSTED_EXTERNAL),
    ("agent_reported_tool_result", AnchorTier.SOFT_SELF_ATTESTED),
    ("server_observation", AnchorTier.EXCLUDED),
    (None, AnchorTier.EXCLUDED),
    ("", AnchorTier.EXCLUDED),
    ("something_new_we_havent_tiered", AnchorTier.EXCLUDED),
])
def test_tier_mapping(source, expected):
    assert tier_for_source(source) is expected


def test_unknown_provenance_is_excluded_not_admitted():
    """Default-deny: a source we have not explicitly tiered must NOT anchor.

    Guards against a new verification_source silently leaking into the anchor
    set (it would have to be added to _TIER_BY_SOURCE deliberately)."""
    assert tier_for_source("future_source") is AnchorTier.EXCLUDED
    assert is_exogenous_anchor("future_source") is False
    assert is_exogenous_anchor("future_source", include_soft=True) is False


def test_self_referential_never_anchors():
    """Invariant 4: the loop observing itself cannot anchor the loop."""
    assert is_exogenous_anchor("server_observation") is False
    assert is_exogenous_anchor("server_observation", include_soft=True) is False
    assert is_exogenous_anchor(None) is False


def test_exogenous_default_is_trusted_only():
    assert is_exogenous_anchor("external_signal") is True
    # soft is NOT admitted by default
    assert is_exogenous_anchor("agent_reported_tool_result") is False


def test_soft_requires_explicit_optin():
    assert is_exogenous_anchor("agent_reported_tool_result", include_soft=True) is True
    # but trusted still passes, and excluded still fails, under opt-in
    assert is_exogenous_anchor("external_signal", include_soft=True) is True
    assert is_exogenous_anchor("server_observation", include_soft=True) is False


def test_sql_predicates_exclude_self_referential():
    assert "server_observation" not in ANCHORED_OUTCOMES_SQL
    assert "server_observation" not in ANCHORED_OUTCOMES_WITH_SOFT_SQL
    assert "verification_source = 'external_signal'" in ANCHORED_OUTCOMES_SQL
    assert anchored_outcomes_predicate() == ANCHORED_OUTCOMES_SQL
    assert anchored_outcomes_predicate(include_soft=True) == ANCHORED_OUTCOMES_WITH_SOFT_SQL
    # the soft predicate admits exactly the two non-excluded sources
    assert "agent_reported_tool_result" in ANCHORED_OUTCOMES_WITH_SOFT_SQL


def test_anchor_predicates_require_joinable_snapshot():
    """Both anchor predicates must AND-in the joinable-snapshot requirement, so a
    snapshot-less row (synthetic harness traffic / non-instrumented agent) cannot
    anchor the residual test (roadmap §6.3)."""
    assert JOINABLE_SNAPSHOT_SQL in ANCHORED_OUTCOMES_SQL
    assert JOINABLE_SNAPSHOT_SQL in ANCHORED_OUTCOMES_WITH_SOFT_SQL
    assert "eisv_e IS NOT NULL" in JOINABLE_SNAPSHOT_SQL
    assert "snapshot_missing" in JOINABLE_SNAPSHOT_SQL


def test_is_anchorable_requires_provenance_and_snapshot():
    # trusted provenance + a real snapshot anchors
    assert is_anchorable("external_signal", eisv_present=True) is True
    # trusted provenance but no snapshot does NOT anchor (the synthetic-smoke case)
    assert is_anchorable("external_signal", eisv_present=False) is False
    # snapshot present but flagged missing does NOT anchor
    assert is_anchorable("external_signal", eisv_present=True, snapshot_missing=True) is False
    # excluded provenance never anchors, snapshot or not
    assert is_anchorable("server_observation", eisv_present=True) is False
    assert is_anchorable(None, eisv_present=True) is False
    # soft self-attested only with explicit opt-in, and still needs a snapshot
    assert is_anchorable("agent_reported_tool_result", eisv_present=True) is False
    assert is_anchorable("agent_reported_tool_result", eisv_present=True, include_soft=True) is True
    assert is_anchorable("agent_reported_tool_result", eisv_present=False, include_soft=True) is False
