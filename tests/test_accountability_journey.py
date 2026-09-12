"""Tests for the deterministic accountability-reconstruction rehearsal."""

from copy import deepcopy

from scripts.eval.accountability_journey import (
    DEFAULT_FIXTURE,
    JourneyError,
    evaluate_journey,
    load_fixture,
    render_report,
    validate_fixture,
)


def test_fixture_reconstructs_the_same_incident_in_both_arms() -> None:
    result = evaluate_journey(load_fixture(DEFAULT_FIXTURE))

    assert result["classification"] == {
        "evidence_class": "mechanism_validation",
        "frozen_protocol_affected": False,
        "headline_eligible": False,
    }
    assert result["semantic_equivalence"] is True
    assert result["failures"] == []
    for arm in result["arms"].values():
        assert arm["metrics"]["reconstruction_accuracy"] == 1.0
        assert arm["metrics"]["attribution_precision"] == 1.0
        assert arm["metrics"]["attribution_coverage"] == 1.0
        assert arm["metrics"]["missing_edge_rate"] == 0.0
        assert arm["manifest"]["exact_oracle_match"] is True


def test_missing_edge_is_reported_instead_of_silently_scoring_complete() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    mutated = deepcopy(fixture)
    mutated["arms"]["unitares"]["records"][1]["edges"] = []

    result = evaluate_journey(mutated)

    assert result["semantic_equivalence"] is False
    assert result["arms"]["unitares"]["metrics"]["missing_edge_rate"] == 0.2
    assert "unitares: 1 oracle edge(s) missing" in result["failures"]


def test_wrong_attribution_is_reported_even_when_answers_are_correct() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    mutated = deepcopy(fixture)
    attribution = mutated["arms"]["structured_handoff"]["records"][0]["attributions"][0]
    attribution["process_id"] = "process-impostor-01"

    result = evaluate_journey(mutated)

    arm = result["arms"]["structured_handoff"]
    assert arm["metrics"]["reconstruction_accuracy"] == 1.0
    assert arm["metrics"]["attribution_precision"] == 0.0
    assert "structured_handoff: 1 effect attribution(s) incorrect" in result["failures"]


def test_missing_attribution_has_coverage_zero_without_inventing_precision() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    mutated = deepcopy(fixture)
    mutated["arms"]["unitares"]["records"][0]["attributions"] = []

    result = evaluate_journey(mutated)

    metrics = result["arms"]["unitares"]["metrics"]
    assert metrics["attribution_precision"] is None
    assert metrics["attribution_coverage"] == 0.0
    assert (
        result["descriptive_deltas_unitares_minus_handoff"]["attribution_precision"]
        is None
    )
    assert "unitares: 1 effect attribution(s) missing" in result["failures"]


def test_fixture_refuses_headline_eligibility() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    fixture["classification"]["headline_eligible"] = True

    try:
        validate_fixture(fixture)
    except JourneyError as exc:
        assert str(exc) == "headline_eligible must be false"
    else:
        raise AssertionError("headline-eligible rehearsal fixture was accepted")


def test_report_states_limits_and_failures() -> None:
    report = render_report(evaluate_journey(load_fixture(DEFAULT_FIXTURE)))

    assert "mechanism validation only" in report
    assert "Headline comparison:** not evaluated" in report
    assert "None in the deterministic fixture." in report
    assert "does not establish that UNITARES improves outcomes" in report


def test_failure_report_refuses_comparison_when_manifests_diverge() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)
    mutated = deepcopy(fixture)
    mutated["arms"]["unitares"]["records"][1]["edges"] = []

    report = render_report(evaluate_journey(mutated))

    assert "Comparative interpretation is invalid" in report
    assert "- unitares: 1 oracle edge(s) missing" in report
