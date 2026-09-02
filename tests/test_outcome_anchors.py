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
    is_structurally_controlled_fixture,
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


@pytest.mark.parametrize("detail", [
    {"synthetic_calibration_fixture": True},
    {"synthetic_negative_control": "yes"},
    {"prediction_binding": "synthetic_negative_control"},
    {"test_name": "overconfidence_probe"},
    '{"test_name":"clean_control"}',
])
def test_structural_fixture_markers_are_shared_by_evidence_consumers(detail):
    assert is_structurally_controlled_fixture(detail) is True


def test_structural_fixture_filter_ignores_mutable_identity_purpose():
    assert is_structurally_controlled_fixture({"purpose": "testing"}) is False
    assert is_structurally_controlled_fixture("not-json") is False


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


def test_table_alias_qualifies_columns():
    """With table_alias, every column ref is prefixed so the predicate can be
    AND-ed into an aliased query (e.g. the skeptic report's `... o`)."""
    p = anchored_outcomes_predicate(table_alias="o")
    assert "o.verification_source" in p
    assert "o.eisv_e" in p
    assert "o.detail->>" in p
    # no bare (unqualified) column tokens leak through
    assert "(verification_source" not in p and " verification_source" not in p
    assert "(eisv_e" not in p
    # no alias requested -> unchanged constant
    assert anchored_outcomes_predicate() == ANCHORED_OUTCOMES_SQL
    assert anchored_outcomes_predicate(include_soft=True, table_alias="o").count("o.eisv_e") == 1


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


def test_exogenous_predicate_is_provenance_only():
    """The behavioural-read predicate must exclude self-referential provenance
    (Invariant 4) WITHOUT requiring a joinable snapshot.

    Consumers that read outcomes as evidence -- behavioral_sensor's _compute_E /
    _compute_I via get_recent_outcomes -- have no residual to join against, so
    AND-ing the §6.3 snapshot clause there would silently discard genuine
    outcomes rather than self-referential ones.
    """
    from src.grounding.outcome_anchors import EXOGENOUS_OUTCOMES_SQL

    # Provenance exclusion IS applied, exogenous tier only.
    assert "external_signal" in EXOGENOUS_OUTCOMES_SQL
    assert "agent_reported_tool_result" not in EXOGENOUS_OUTCOMES_SQL
    assert "server_observation" not in EXOGENOUS_OUTCOMES_SQL

    # Snapshot requirement is NOT applied -- this is the whole point.
    assert "eisv_e" not in EXOGENOUS_OUTCOMES_SQL
    assert "snapshot_missing" not in EXOGENOUS_OUTCOMES_SQL


def test_exogenous_predicate_excludes_soft_and_self_referential():
    """It admits ONLY the trusted-external tier.

    Deliberately not the complement of EXCLUDED_OUTCOMES_SQL: the soft
    self-attested tier is neither an anchor nor admissible into a verdict
    input, because the live consumer ignores evidence_weight and would count a
    self-report as heavily as a CI result.
    """
    from src.grounding.outcome_anchors import (
        EXOGENOUS_OUTCOMES_SQL,
        AnchorTier,
        tier_for_source,
    )

    assert "external_signal" in EXOGENOUS_OUTCOMES_SQL
    assert "agent_reported_tool_result" not in EXOGENOUS_OUTCOMES_SQL
    assert "server_observation" not in EXOGENOUS_OUTCOMES_SQL

    assert tier_for_source("external_signal") is AnchorTier.TRUSTED_EXTERNAL
    assert tier_for_source("agent_reported_tool_result") is AnchorTier.SOFT_SELF_ATTESTED
    for excluded in ("server_observation", "made_up_source", None):
        assert tier_for_source(excluded) is AnchorTier.EXCLUDED


# --- Fixture rules (2026-09-02) ---------------------------------------------
# ``calibration_excluded`` is stamped for three causes; only two are fixture
# traffic. The corrected rule keeps rows whose only exclusion is a scraped
# confidence; the registered rule (the pre-registered read's predicate) drops
# them. See docs/proposals/outcome-fixture-conflation-decision-packet-v0.md.


def test_scraped_only_rows_are_fixtures_under_registered_but_not_corrected():
    from src.grounding.outcome_anchors import is_scraped_only_exclusion

    scraped = {
        "calibration_excluded": True,
        "prediction_source": "audit_trail_fallback",
        "producer": "ci",
    }
    assert is_structurally_controlled_fixture(scraped, rule="registered") is True
    assert is_structurally_controlled_fixture(scraped, rule="corrected") is False
    assert is_structurally_controlled_fixture(scraped) is True  # registered is the default
    assert is_scraped_only_exclusion(scraped) is True
    # A row that was never excluded is not scraped-only, whatever its source.
    assert is_scraped_only_exclusion({"calibration_excluded": False, "prediction_source": "audit_trail_fallback"}) is False


def test_exclusion_reasons_decide_rows_that_carry_them():
    scraped = {"calibration_excluded": True, "calibration_exclusion_reasons": ["scraped_confidence"]}
    both = {
        "calibration_excluded": True,
        "calibration_exclusion_reasons": ["scraped_confidence", "shadow_write"],
    }
    fixture = {"calibration_excluded": True, "calibration_exclusion_reasons": ["controlled_fixture"]}
    assert is_structurally_controlled_fixture(scraped, rule="corrected") is False
    assert is_structurally_controlled_fixture(both, rule="corrected") is True
    assert is_structurally_controlled_fixture(fixture, rule="corrected") is True
    # A reasons list trumps a scraped prediction_source on the same row.
    assert is_structurally_controlled_fixture(
        {**fixture, "prediction_source": "audit_trail_fallback"}, rule="corrected"
    ) is True


def test_explicit_markers_and_shadow_write_win_over_a_scraped_source():
    rows = [
        {"synthetic_calibration_fixture": True, "prediction_source": "prev_confidence_fallback"},
        {"calibration_excluded": True, "shadow_write": True, "prediction_source": "prev_confidence_fallback"},
        {
            "calibration_excluded": True,
            "prediction_binding": "synthetic_negative_control",
            "prediction_source": "audit_trail_fallback",
        },
        {"calibration_excluded": True, "test_name": "clean_control", "prediction_source": "audit_trail_fallback"},
    ]
    for row in rows:
        assert is_structurally_controlled_fixture(row, rule="corrected") is True
        assert is_structurally_controlled_fixture(row, rule="registered") is True


def test_bare_flag_without_scraped_evidence_stays_a_fixture_under_both_rules():
    from src.grounding.outcome_anchors import FIXTURE_RULES

    for rule in FIXTURE_RULES:
        assert is_structurally_controlled_fixture({"calibration_excluded": True}, rule=rule) is True


def test_unknown_fixture_rule_fails_closed():
    from src.grounding.outcome_anchors import normalize_fixture_rule

    with pytest.raises(ValueError):
        is_structurally_controlled_fixture({"calibration_excluded": True}, rule="lenient")
    with pytest.raises(ValueError):
        normalize_fixture_rule("")
    assert normalize_fixture_rule(" Registered ") == "registered"


def test_historical_bare_flag_plus_scraped_source_is_classified_as_evidence():
    """The one ambiguous historical shape, stated rather than hidden.

    Before reasons were stamped, a caller-supplied bare `calibration_excluded`
    (a fixture whose only marker is the flag) that was ALSO scraped is
    indistinguishable from a scraped-only row. The corrected rule reads it as
    evidence; the registered rule drops it. Rows written after this change
    carry `calibration_exclusion_reasons` and are not ambiguous. The live
    count of this shape is reported in the PR that shipped the rule.
    """
    ambiguous = {"calibration_excluded": True, "prediction_source": "prev_confidence_fallback"}
    assert is_structurally_controlled_fixture(ambiguous, rule="registered") is True
    assert is_structurally_controlled_fixture(ambiguous, rule="corrected") is False
    stamped = {**ambiguous, "calibration_exclusion_reasons": ["controlled_fixture", "scraped_confidence"]}
    assert is_structurally_controlled_fixture(stamped, rule="corrected") is True
