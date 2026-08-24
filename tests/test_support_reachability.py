"""Tests for the stop-rule support-condition diagnostic."""

from __future__ import annotations

from datetime import date, timedelta
import json

import pytest

from scripts.analysis.support_reachability import (
    FROZEN_WINDOW_COUNTS_BY_LEAD,
    INSUFFICIENT_LONGITUDINAL_EVIDENCE,
    READ_DATE,
    REGISTERED_COHORT_START,
    TARGET_BAD_CLUSTERS,
    _jsonable,
    frozen_diagnostic,
    longitudinal_count_change_comparison,
    lookback_comparison,
    main,
    support_requirement,
)


def test_frozen_default_fails_toward_unknown_and_preserves_both_leads():
    diagnostic = frozen_diagnostic()

    assert diagnostic["verdict"] == INSUFFICIENT_LONGITUDINAL_EVIDENCE
    lead_0 = diagnostic["requirements_by_lead_minutes"][0]
    lead_30 = diagnostic["requirements_by_lead_minutes"][30]
    assert (
        lead_0.observed_blocks,
        lead_0.conditional_gap_if_no_older_blocks,
    ) == (29, 121)
    assert (
        lead_30.observed_blocks,
        lead_30.conditional_gap_if_no_older_blocks,
    ) == (28, 122)
    assert lead_0.conditional_required_per_month == pytest.approx(32.31, abs=0.01)
    assert lead_30.conditional_required_per_month == pytest.approx(32.57, abs=0.01)


def test_frozen_counts_match_the_two_registered_leads_and_windows():
    assert FROZEN_WINDOW_COUNTS_BY_LEAD == {
        0: {30: 29, 90: 29},
        30: {30: 28, 90: 28},
    }
    assert TARGET_BAD_CLUSTERS == 150
    assert READ_DATE == date(2026, 12, 1)
    assert REGISTERED_COHORT_START == date(2025, 12, 1)


@pytest.mark.parametrize("observed", [-1, False, 1.5])
def test_support_requirement_rejects_invalid_counts(observed):
    with pytest.raises(ValueError):
        support_requirement(observed)


def test_support_requirement_rejects_a_post_read_count():
    with pytest.raises(ValueError, match="registered read date"):
        support_requirement(151, observed_through=READ_DATE + timedelta(days=1))


def test_equal_lookbacks_describe_only_the_observed_annulus():
    comparison = lookback_comparison({30: 28, 90: 28})

    assert comparison.distinct_cluster_keys_added == 0
    assert comparison.lookback_invariant_at_cutoff is True
    assert (
        comparison.verdict == "NO_ADDITIONAL_DISTINCT_CLUSTER_KEYS_WITH_WIDER_LOOKBACK"
    )
    assert "do not imply an empty older annulus" in comparison.reading
    assert "does not estimate future accrual" in comparison.reading
    assert "365-day count" in comparison.reading
    assert "supply-limited" not in comparison.reading


def test_wider_lookback_can_report_additional_blocks_without_forecasting():
    comparison = lookback_comparison({30: 28, 90: 84})

    assert comparison.distinct_cluster_keys_added == 56
    assert comparison.lookback_invariant_at_cutoff is False
    assert comparison.verdict == "ADDITIONAL_DISTINCT_CLUSTER_KEYS_WITH_WIDER_LOOKBACK"
    assert "does not estimate future accrual" in comparison.reading


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {30: 28},
        {0: 0, 30: 1},
        {False: 0, 30: 1},
        {30: -1, 90: 0},
        {30: False, 90: 0},
        {30: 29, 90: 28},
    ],
)
def test_invalid_lookback_inputs_are_rejected(counts):
    with pytest.raises(ValueError):
        lookback_comparison(counts)


def _paired_scenario(
    *,
    start_counts: dict[int, int] | None = None,
    end_counts: dict[int, int] | None = None,
    fixed_cohort_start: date = REGISTERED_COHORT_START,
    start_date: date = date(2026, 5, 15),
    end_date: date = date(2026, 8, 23),
    fingerprint: str = "task-trusted-window-start-2025-12-01T16:00:00Z-v1",
):
    return longitudinal_count_change_comparison(
        cohort_fingerprint=fingerprint,
        fixed_cohort_start=fixed_cohort_start,
        start_counts_by_lead=({0: 0, 30: 0} if start_counts is None else start_counts),
        start_date=start_date,
        end_counts_by_lead=({0: 76, 30: 74} if end_counts is None else end_counts),
        end_date=end_date,
    )


def test_paired_probe_is_bidirectional_and_allows_leads_to_diverge():
    scenario = _paired_scenario()
    lead_0 = scenario.count_change_by_lead_minutes[0]
    lead_30 = scenario.count_change_by_lead_minutes[30]

    assert set(scenario.count_change_by_lead_minutes) == {0, 30}
    assert lead_0.observed_days == lead_30.observed_days == 100
    assert lead_0.remaining_days == lead_30.remaining_days == 100
    assert lead_0.acceleration_required == pytest.approx(0.97)
    assert lead_0.verdict == "SUPPLIED_NET_CHANGE_PACE_WOULD_SUFFICE"
    assert lead_30.acceleration_required == pytest.approx(1.03)
    assert lead_30.verdict == "SUPPLIED_NET_CHANGE_PACE_WOULD_NOT_SUFFICE"


def test_paired_probe_treats_the_exact_rate_boundary_as_sufficient():
    scenario = _paired_scenario(end_counts={0: 75, 30: 75})

    for rate in scenario.count_change_by_lead_minutes.values():
        assert rate.acceleration_required == 1.0
        assert rate.verdict == "SUPPLIED_NET_CHANGE_PACE_WOULD_SUFFICE"


def test_paired_probe_uses_net_eligible_count_change_not_ending_stock():
    scenario = _paired_scenario(
        start_counts={0: 28, 30: 27},
        end_counts={0: 60, 30: 60},
        fixed_cohort_start=REGISTERED_COHORT_START,
        start_date=date(2026, 8, 9),
        end_date=date(2026, 10, 1),
    )

    assert scenario.count_change_by_lead_minutes[0].net_eligible_count_change == 32
    assert scenario.count_change_by_lead_minutes[30].net_eligible_count_change == 33
    assert scenario.count_change_by_lead_minutes[
        0
    ].net_change_per_month == pytest.approx(18.38, abs=0.01)
    assert "registered 365-day lower bound" in scenario.notes[1]
    assert "not necessarily event accrual" in scenario.notes[2]


def test_paired_probe_can_return_met_and_unmet_for_different_leads():
    scenario = _paired_scenario(
        start_counts={0: 140, 30: 100},
        end_counts={0: 151, 30: 110},
    )

    assert (
        scenario.count_change_by_lead_minutes[0].verdict
        == "TARGET_ALREADY_MET_AT_SECOND_CENSUS"
    )
    assert (
        scenario.count_change_by_lead_minutes[30].verdict
        == "SUPPLIED_NET_CHANGE_PACE_WOULD_NOT_SUFFICE"
    )
    assert "planning scenario" in scenario.notes[-1]


def test_read_date_below_target_uses_null_instead_of_infinity():
    scenario = _paired_scenario(
        start_counts={0: 28, 30: 28},
        end_counts={0: 60, 30: 60},
        start_date=date(2026, 8, 9),
        end_date=READ_DATE,
    )

    for rate in scenario.count_change_by_lead_minutes.values():
        assert rate.verdict == "REGISTERED_READ_REACHED_BELOW_TARGET"
        assert rate.required_per_month is None
        assert rate.acceleration_required is None
    encoded = json.dumps(_jsonable(scenario), allow_nan=False)
    assert "Infinity" not in encoded
    assert '"required_per_month": null' in encoded


def test_zero_net_eligible_count_change_does_not_divide_by_zero():
    scenario = _paired_scenario(
        start_counts={0: 28, 30: 28},
        end_counts={0: 28, 30: 28},
    )

    for rate in scenario.count_change_by_lead_minutes.values():
        assert rate.verdict == "NO_NET_ELIGIBLE_COUNT_CHANGE"
        assert rate.net_change_per_month == 0.0
        assert rate.acceleration_required is None


def _valid_longitudinal_kwargs():
    return {
        "cohort_fingerprint": "task-trusted-window-start-2025-12-01T16:00:00Z-v1",
        "fixed_cohort_start": REGISTERED_COHORT_START,
        "start_counts_by_lead": {0: 10, 30: 10},
        "start_date": date(2026, 8, 1),
        "end_counts_by_lead": {0: 12, 30: 12},
        "end_date": date(2026, 8, 9),
    }


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("cohort_fingerprint", "  "),
        ("cohort_fingerprint", None),
        ("fixed_cohort_start", REGISTERED_COHORT_START - timedelta(days=1)),
        ("fixed_cohort_start", REGISTERED_COHORT_START + timedelta(days=1)),
        ("start_date", REGISTERED_COHORT_START - timedelta(days=1)),
        ("start_counts_by_lead", {0: 10}),
        ("start_counts_by_lead", {0: 10, 30: 10, 5: 10}),
        ("start_counts_by_lead", {False: 10, 30: 10}),
        ("start_counts_by_lead", {0: False, 30: 10}),
        ("end_counts_by_lead", {0: 12}),
        ("end_counts_by_lead", {0: 9, 30: 12}),
        ("end_date", date(2026, 8, 1)),
        ("end_date", date(2026, 7, 31)),
        ("end_date", date(2026, 12, 2)),
    ],
)
def test_invalid_longitudinal_inputs_are_rejected(field, invalid):
    kwargs = _valid_longitudinal_kwargs()
    kwargs[field] = invalid
    with pytest.raises(ValueError):
        longitudinal_count_change_comparison(**kwargs)


def test_post_read_success_cannot_be_backdated_into_the_registered_gate():
    kwargs = _valid_longitudinal_kwargs()
    kwargs["end_counts_by_lead"] = {0: 151, 30: 151}
    kwargs["end_date"] = date(2026, 12, 15)

    with pytest.raises(ValueError, match="registered read date"):
        longitudinal_count_change_comparison(**kwargs)


def test_default_cli_reports_unknown_without_the_withdrawn_claim(capsys):
    assert main([]) == 0
    output = capsys.readouterr().out

    assert INSUFFICIENT_LONGITUDINAL_EVIDENCE in output
    assert "lead 0m: 29 bad clusters; conditional gap 121" in output
    assert "lead 30m: 28 bad clusters; conditional gap 122" in output
    assert "9.7" not in output
    assert "supply-limited" not in output
    assert "registered read remains in force" in output


def test_default_cli_json_is_strict_and_preserves_unknown_status(capsys):
    assert main(["--json"]) == 0
    output = capsys.readouterr().out

    def reject_nonstandard_constant(value):
        raise ValueError(value)

    payload = json.loads(output, parse_constant=reject_nonstandard_constant)
    assert payload["verdict"] == INSUFFICIENT_LONGITUDINAL_EVIDENCE
    lead_0 = payload["requirements_by_lead_minutes"]["0"]
    assert lead_0["observed_blocks"] == 29
    assert lead_0["conditional_gap_if_no_older_blocks"] == 121
    assert "Infinity" not in output


def test_cli_rejects_longitudinal_overrides(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--start-blocks", "28"])

    assert exc_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
