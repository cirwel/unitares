from scripts.analysis.outcome_inventory import (
    OutcomeInventoryRow,
    build_inventory,
    format_inventory_report,
    is_controlled_validation_fixture,
)


def test_build_inventory_groups_by_scope_type_source_and_evidence_flags():
    rows = [
        OutcomeInventoryRow(
            outcome_type="test_passed",
            is_bad=False,
            verification_source="server_observation",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "prediction_binding": "registry",
                "prediction_id": "pred-1",
            },
            prior_state_by_lead={0.0: True, 5.0: True, 30.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="test_failed",
            is_bad=True,
            verification_source="server_observation",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "prediction_binding": "registry",
                "prediction_id": "pred-2",
            },
            prior_state_by_lead={0.0: True, 5.0: False, 30.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="task_failed",
            is_bad=True,
            verification_source="agent_reported_tool_result",
            detail={
                "hard_exogenous": False,
                "eprocess_eligible": False,
                "prediction_binding": "prev_confidence_fallback",
            },
            prior_state_by_lead={0.0: True, 5.0: True, 30.0: True},
        ),
        OutcomeInventoryRow(
            outcome_type="drawing_completed",
            is_bad=False,
            verification_source=None,
            detail={"verification_source": "external_signal"},
            prior_state_by_lead={0.0: False, 5.0: False, 30.0: False},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0, 5.0, 30.0))
    by_key = {
        (
            bucket.scope,
            bucket.outcome_type,
            bucket.verification_source,
            bucket.hard_exogenous,
            bucket.eprocess_eligible,
            bucket.prediction_binding,
        ): bucket
        for bucket in inventory.buckets
    }

    strict_tests = by_key[(
        "strict",
        "test_passed/test_failed",
        "server_observation",
        True,
        True,
        "registry",
    )]
    assert strict_tests.n_total == 2
    assert strict_tests.n_bad == 1
    assert strict_tests.bad_rate == 0.5
    assert strict_tests.prior_state_counts == {0.0: 2, 5.0: 1, 30.0: 0}
    assert strict_tests.prediction_id_count == 2

    task_failed = by_key[(
        "task",
        "task_failed",
        "agent_reported_tool_result",
        False,
        False,
        "prev_confidence_fallback",
    )]
    assert task_failed.n_total == 1
    assert task_failed.n_bad == 1
    assert task_failed.prior_state_counts == {0.0: 1, 5.0: 1, 30.0: 1}

    other = by_key[("other", "drawing_completed", "external_signal", False, False, "none")]
    assert other.n_total == 1
    assert inventory.total_outcomes == 4
    assert inventory.total_bad == 2
    assert inventory.total_prediction_id_count == 2


def test_build_inventory_splits_beam_harness_from_substrate_lane():
    rows = [
        OutcomeInventoryRow(
            outcome_type="task_completed",
            is_bad=False,
            verification_source="external_signal",
            detail={"hard_exogenous": True, "eprocess_eligible": True, "harness": "beam"},
            prior_state_by_lead={0.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="task_completed",
            is_bad=False,
            verification_source="external_signal",
            detail={"hard_exogenous": True, "eprocess_eligible": True},
            prior_state_by_lead={0.0: True},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0,))
    by_lane = {bucket.harness_lane: bucket for bucket in inventory.buckets}

    assert set(by_lane) == {"beam", "substrate"}
    assert by_lane["beam"].n_total == 1
    assert by_lane["beam"].prior_state_counts == {0.0: 0}
    assert by_lane["substrate"].n_total == 1
    assert by_lane["substrate"].prior_state_counts == {0.0: 1}
    assert inventory.eprocess_eligible_by_harness_lane == {"beam": 1, "substrate": 1}


def test_format_inventory_report_exposes_zero_bad_strict_and_prior_coverage():
    rows = [
        OutcomeInventoryRow(
            outcome_type="test_passed",
            is_bad=False,
            verification_source="server_observation",
            detail={"hard_exogenous": True, "eprocess_eligible": True},
            prior_state_by_lead={0.0: True, 5.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="task_failed",
            is_bad=True,
            verification_source="agent_reported_tool_result",
            detail={"hard_exogenous": False, "eprocess_eligible": False},
            prior_state_by_lead={0.0: True, 5.0: True},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0, 5.0))
    report = format_inventory_report(inventory, window_days=30, lead_minutes=(0.0, 5.0))

    assert "Outcome Inventory" in report
    assert "total_outcomes: 2" in report
    assert "strict_outcomes: 1" in report
    assert "strict_bad: 0" in report
    assert "task_failed" in report
    assert "prior_state_5m" in report
    assert "agent_reported_tool_result" in report


def test_controlled_validation_fixture_detection_covers_legacy_and_new_markers():
    assert is_controlled_validation_fixture({"test_name": "overconfidence_probe"})
    assert is_controlled_validation_fixture({"synthetic_calibration_fixture": True})
    assert is_controlled_validation_fixture({"do_not_use_for_live_validation": True})
    assert is_controlled_validation_fixture({"prediction_binding": "synthetic_negative_control"})
    assert is_controlled_validation_fixture({"calibration_excluded": True})
    assert not is_controlled_validation_fixture({"test_name": "real_pytest_suite"})
