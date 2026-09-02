import asyncio
import pytest
from scripts.analysis import outcome_inventory as inventory_module
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

    strict_tests = by_key[
        (
            "strict",
            "test_passed/test_failed",
            "server_observation",
            True,
            True,
            "registry",
        )
    ]
    assert strict_tests.n_total == 2
    assert strict_tests.n_bad == 1
    assert strict_tests.bad_rate == 0.5
    assert strict_tests.prior_state_counts == {0.0: 2, 5.0: 1, 30.0: 0}
    assert strict_tests.prediction_id_count == 2

    task_failed = by_key[
        (
            "task",
            "task_failed",
            "agent_reported_tool_result",
            False,
            False,
            "prev_confidence_fallback",
        )
    ]
    assert task_failed.n_total == 1
    assert task_failed.n_bad == 1
    assert task_failed.prior_state_counts == {0.0: 1, 5.0: 1, 30.0: 1}

    other = by_key[
        ("other", "drawing_completed", "external_signal", False, False, "none")
    ]
    assert other.n_total == 1
    assert inventory.total_outcomes == 4
    assert inventory.total_bad == 2
    assert inventory.total_prediction_id_count == 2
    assert inventory.registry_prediction_bound_count == 2


def test_build_inventory_counts_registry_prediction_bindings_separately_from_ids():
    rows = [
        OutcomeInventoryRow(
            outcome_type="test_passed",
            is_bad=False,
            verification_source="server_observation",
            detail={"prediction_id": "pred-reg", "prediction_binding": "registry"},
            prior_state_by_lead={0.0: True},
        ),
        OutcomeInventoryRow(
            outcome_type="test_failed",
            is_bad=True,
            verification_source="server_observation",
            detail={"prediction_id": "pred-missing", "prediction_binding": "missing_prediction"},
            prior_state_by_lead={0.0: True},
        ),
        OutcomeInventoryRow(
            outcome_type="task_completed",
            is_bad=False,
            verification_source="agent_reported_tool_result",
            detail={"prediction_binding": "argument_fallback"},
            prior_state_by_lead={0.0: True},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0,))

    assert inventory.total_prediction_id_count == 2
    assert inventory.registry_prediction_bound_count == 1
    assert inventory.registry_prediction_bound_by_harness_lane == {"substrate": 1}


def test_format_inventory_report_exposes_registry_bound_prediction_count():
    rows = [
        OutcomeInventoryRow(
            outcome_type="test_passed",
            is_bad=False,
            verification_source="server_observation",
            detail={"prediction_id": "pred-reg", "prediction_binding": "registry"},
            prior_state_by_lead={0.0: True},
        ),
        OutcomeInventoryRow(
            outcome_type="test_failed",
            is_bad=True,
            verification_source="server_observation",
            detail={"prediction_id": "pred-missing", "prediction_binding": "missing_prediction"},
            prior_state_by_lead={0.0: True},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0,))
    report = format_inventory_report(inventory, window_days=90, lead_minutes=(0.0,))

    assert "prediction_id_present: 2" in report
    assert "registry_prediction_bound: 1" in report
    assert (
        "| Lane | Outcomes | Bad | Strict outcomes | Strict bad | Task-scope outcomes | Task-scope bad | E-process eligible | Prediction IDs | Registry-bound predictions | Prior state 0m |"
        in report
    )
    assert "| substrate | 2 | 1 | 2 | 1 | 2 | 1 | 0 | 2 | 1 | 2/2 |" in report


def test_build_inventory_splits_beam_harness_from_substrate_lane():
    rows = [
        OutcomeInventoryRow(
            outcome_type="task_completed",
            is_bad=False,
            verification_source="external_signal",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "harness": "beam",
            },
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


def test_format_inventory_report_includes_harness_lane_summary():
    rows = [
        OutcomeInventoryRow(
            outcome_type="task_completed",
            is_bad=False,
            verification_source="external_signal",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "harness": "beam",
            },
            prior_state_by_lead={0.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="task_failed",
            is_bad=True,
            verification_source="external_signal",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "harness": "beam",
            },
            prior_state_by_lead={0.0: False},
        ),
        OutcomeInventoryRow(
            outcome_type="test_passed",
            is_bad=False,
            verification_source="server_observation",
            detail={
                "hard_exogenous": True,
                "eprocess_eligible": True,
                "prediction_id": "pred-1",
            },
            prior_state_by_lead={0.0: True},
        ),
        OutcomeInventoryRow(
            outcome_type="task_failed",
            is_bad=True,
            verification_source="agent_reported_tool_result",
            detail={"hard_exogenous": False, "eprocess_eligible": False},
            prior_state_by_lead={0.0: True},
        ),
    ]

    inventory = build_inventory(rows, lead_minutes=(0.0,))
    report = format_inventory_report(inventory, window_days=90, lead_minutes=(0.0,))

    assert "## Harness Lane Summary" in report
    assert (
        "| Lane | Outcomes | Bad | Strict outcomes | Strict bad | Task-scope outcomes | Task-scope bad | E-process eligible | Prediction IDs | Registry-bound predictions | Prior state 0m |"
        in report
    )
    assert "| beam | 2 | 1 | 0 | 0 | 2 | 1 | 2 | 0 | 0 | 0/2 |" in report
    assert "| substrate | 2 | 1 | 1 | 0 | 2 | 1 | 1 | 1 | 0 | 2/2 |" in report


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
    assert "strict_bad_min_for_validation: 10" in report
    assert "strict_bad_gap_to_min: 10" in report
    assert "`bad` is an outcome-label class (`is_bad=true`)" in report
    assert "not a moral verdict or a prevented outcome" in report
    assert "online agent-state estimation (agent proprioception)" in report
    assert "not a bad-verdict dispenser" in report
    assert "task_failed" in report
    assert "prior_state_5m" in report
    assert "agent_reported_tool_result" in report


def test_controlled_validation_fixture_detection_covers_legacy_and_new_markers():
    assert is_controlled_validation_fixture({"test_name": "overconfidence_probe"})
    assert is_controlled_validation_fixture({"synthetic_calibration_fixture": True})
    assert is_controlled_validation_fixture({"do_not_use_for_live_validation": True})
    assert is_controlled_validation_fixture(
        {"prediction_binding": "synthetic_negative_control"}
    )
    assert is_controlled_validation_fixture({"calibration_excluded": True})
    assert not is_controlled_validation_fixture({"test_name": "real_pytest_suite"})


def test_controlled_validation_fixture_detection_covers_demo_perf_identity_metadata():
    assert is_controlled_validation_fixture(
        {"_identity_metadata": {"label": "quick-demo-agent_6d051ff8"}}
    )
    assert is_controlled_validation_fixture(
        {"_identity_metadata": {"label": "perf-profile-checkin_be34425f"}}
    )
    assert is_controlled_validation_fixture(
        {"_identity_metadata": {"purpose": "testing", "label": "demo-harness"}}
    )
    assert not is_controlled_validation_fixture(
        {"_identity_metadata": {"purpose": "implementation", "label": "real-agent"}}
    )


def test_inventory_record_conversion_preserves_identity_metadata_for_fixture_filtering():
    row = inventory_module._row_from_record(
        {
            "outcome_type": "task_failed",
            "is_bad": True,
            "verification_source": "agent_reported_tool_result",
            "detail": {"source": "auto_checkin"},
            "identity_metadata": {"label": "quick-demo-agent_abc123"},
            "prior_state_lead_0": False,
        },
        (0.0,),
    )

    assert row.detail["_identity_metadata"] == {"label": "quick-demo-agent_abc123"}
    assert is_controlled_validation_fixture(row.detail)


def test_declared_purpose_clause_is_gateable_for_validation_slices():
    """`purpose` is agent-supplied free text, so it must not gate validation.

    Excluding on it lets the subject of the measurement opt out of being
    measured. Live, that clause removed 47% of a strict-scope window -- bad
    labels included -- and reversed the sign of the measured lift, so predictive
    slices pass include_declared_purpose=False while inventory reporting keeps
    the default.
    """
    declared_only = {"_identity_metadata": {"purpose": "testing", "label": "real-agent"}}

    assert is_controlled_validation_fixture(declared_only)
    assert not is_controlled_validation_fixture(
        declared_only, include_declared_purpose=False
    )
    assert inventory_module.is_declared_non_production_purpose(declared_only)

    # Structural markers still apply with the clause disabled.
    structural = {
        "_identity_metadata": {"purpose": "testing", "label": "quick-demo-agent_6d05"}
    }
    assert is_controlled_validation_fixture(
        structural, include_declared_purpose=False
    )
    assert is_controlled_validation_fixture(
        {"synthetic_calibration_fixture": True}, include_declared_purpose=False
    )


def test_calibration_excluded_only_is_separable_from_real_fixtures():
    """#1790: the server-stamped scraped-confidence flag must be attributable
    as its own attrition class, not folded silently into fixture traffic."""
    from scripts.analysis.outcome_inventory import (
        _fixture_only_because_calibration_excluded,
        is_controlled_validation_fixture,
    )

    # The server stamps the flag together with the prediction_source it scraped from.
    scraped_only = {"calibration_excluded": True, "producer": "ci", "prediction_source": "prev_confidence_fallback"}
    assert is_controlled_validation_fixture(scraped_only) is True
    assert _fixture_only_because_calibration_excluded(scraped_only) is True

    real_fixture = {
        "calibration_excluded": True,
        "synthetic_calibration_fixture": True,
    }
    assert is_controlled_validation_fixture(real_fixture) is True
    assert _fixture_only_because_calibration_excluded(real_fixture) is False
    # A bare flag with no scrape evidence at all (no reasons, no prediction_source)
    # can only be caller-supplied, so it is a fixture, not a scraped-confidence row.
    assert _fixture_only_because_calibration_excluded({"calibration_excluded": True, "producer": "ci"}) is False

    visible = {"producer": "ci", "calibration_excluded": False}
    assert is_controlled_validation_fixture(visible) is False


def test_corrected_rule_keeps_scraped_only_rows_and_registered_rule_drops_them():
    """2026-09-02: the #1790 population is validation-visible under the corrected rule."""
    from scripts.analysis.outcome_inventory import (
        _fixture_only_because_calibration_excluded,
        is_controlled_validation_fixture,
    )

    scraped = {
        "calibration_excluded": True,
        "prediction_source": "audit_trail_fallback",
        "producer": "ci",
    }
    assert is_controlled_validation_fixture(scraped, rule="registered") is True
    assert is_controlled_validation_fixture(scraped, rule="corrected") is False
    assert is_controlled_validation_fixture(scraped) is True  # registered is the default
    assert _fixture_only_because_calibration_excluded(scraped) is True
    # Phase-5 shadow rows are excluded for a different reason and stay out of the bucket.
    shadow = {"calibration_excluded": True, "shadow_write": True, "prediction_source": "registry"}
    assert _fixture_only_because_calibration_excluded(shadow) is False
    # A bare-flag fixture whose reasons name a controlled fixture is not scraped-only either.
    reasons_fixture = {"calibration_excluded": True, "calibration_exclusion_reasons": ["controlled_fixture"]}
    assert _fixture_only_because_calibration_excluded(reasons_fixture) is False
    assert is_controlled_validation_fixture(reasons_fixture, rule="corrected") is True
    # Nor is a scraped row that also declares a validation purpose: the wrapper
    # marks it a fixture under both rules, so it is not in the bucket either.
    purposed = {"calibration_excluded": True, "prediction_source": "audit_trail_fallback", "purpose": "validation"}
    if is_controlled_validation_fixture(purposed, rule="corrected"):
        assert _fixture_only_because_calibration_excluded(purposed) is False

    real_fixture = {
        "calibration_excluded": True,
        "synthetic_calibration_fixture": True,
        "prediction_source": "audit_trail_fallback",
    }
    assert is_controlled_validation_fixture(real_fixture, rule="corrected") is True
    assert _fixture_only_because_calibration_excluded(real_fixture) is False


def test_parse_args_fixture_rule_defaults_to_registered():
    assert inventory_module.parse_args([]).fixture_rule == "registered"
    assert inventory_module.parse_args(["--fixture-rule", "corrected"]).fixture_rule == "corrected"


class _FakeAsyncpgModule:
    """Minimal asyncpg stand-in so fetch_rows can be exercised without a database."""

    def __init__(self, records):
        self._records = records

    async def connect(self, _dsn):
        records = self._records

        class _Conn:
            async def fetch(self, *_args):
                return records

            async def close(self):
                return None

        return _Conn()


def _inventory_record(detail):
    return {
        "outcome_type": "test_failed",
        "is_bad": True,
        "verification_source": "external_signal",
        "detail": detail,
        "identity_metadata": None,
        "prior_state_lead_0": True,
    }


@pytest.mark.parametrize("rule, kept, scraped_kept", [("registered", 1, 0), ("corrected", 2, 1)])
def test_fetch_rows_attrition_reports_both_rules(monkeypatch, rule, kept, scraped_kept):
    """The attrition counters partition the same rows under either rule."""
    import importlib

    records = [
        _inventory_record({"kind": "live"}),
        _inventory_record({"calibration_excluded": True, "prediction_source": "audit_trail_fallback"}),
        _inventory_record({"calibration_excluded": True, "synthetic_calibration_fixture": True}),
        _inventory_record({"calibration_excluded": True, "shadow_write": True, "prediction_source": "registry"}),
        # A bare-flag fixture that says why: excluded under both rules, not scraped-only.
        _inventory_record({"calibration_excluded": True, "calibration_exclusion_reasons": ["controlled_fixture"]}),
    ]
    fake = _FakeAsyncpgModule(records)
    real_import = importlib.import_module
    monkeypatch.setattr(
        inventory_module.importlib, "import_module",
        lambda name, *a, **k: fake if name == "asyncpg" else real_import(name, *a, **k),
    )
    attrition: dict = {}
    rows = asyncio.run(
        inventory_module.fetch_rows(
            "postgresql://unused", window_days=21, lead_minutes=(0.0,),
            attrition=attrition, fixture_rule=rule,
        )
    )
    assert len(rows) == kept
    assert attrition["fixture_rule"] == rule
    assert attrition["fixture_rows_excluded"] == 5 - kept
    assert attrition["calibration_excluded_only"] == 1  # the scraped row only
    assert attrition["scraped_only_rows_kept"] == scraped_kept


def test_report_names_the_fixture_rule_even_when_nothing_was_excluded(monkeypatch, tmp_path, capsys):
    import importlib

    fake = _FakeAsyncpgModule([_inventory_record({"kind": "live"})])
    real_import = importlib.import_module
    monkeypatch.setattr(
        inventory_module.importlib, "import_module",
        lambda name, *a, **k: fake if name == "asyncpg" else real_import(name, *a, **k),
    )
    monkeypatch.setenv("GOVERNANCE_DATABASE_URL", "postgresql://unused")
    out = tmp_path / "inventory.md"
    rc = inventory_module.main(["--window-days", "21", "--leads", "0", "--fixture-rule", "corrected", "--output", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "fixture_rule: corrected" in text
    assert "fixture_rows_excluded: 0" in text
