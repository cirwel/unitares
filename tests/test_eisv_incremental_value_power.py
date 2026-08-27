"""Tests for the planning-only EISV paired-Brier power estimate."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from scripts.analysis import eisv_incremental_value_contract as contract
from scripts.analysis import eisv_incremental_value_power as power


@pytest.fixture
def scenario() -> dict[str, Any]:
    return {
        "scenario_id": "test",
        "alpha_two_sided": 0.05,
        "target_power": 0.8,
        "baseline_brier": 0.2,
        "minimum_relative_brier_improvement": 0.1,
        "paired_loss_difference_sd": 0.2,
        "censored_fraction": 0.0,
        "unscorable_fraction_of_uncensored": 0.0,
        "primary_adverse_rate": 0.25,
        "independence_unit_icc": 0.0,
        "task_icc": 0.0,
        "independence_unit_cluster_sizes": [1, 1],
        "task_cluster_sizes": [1, 1],
    }


def test_iid_formula_and_planning_only_status(scenario: dict[str, Any]) -> None:
    report = power.plan_scenario(scenario)

    assert report["status"] == "PLANNING_ONLY"
    assert report["decision_authority"] == "NONE"
    assert report["combined_design_effect"] == 1.0
    assert report["required_episode_denominator"] == pytest.approx(
        report["iid_usable_episode_bound"], abs=1.0
    )
    assert report["denominator_rule"] == "CENSORED_AND_UNSCORABLE_RETAINED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paired_loss_difference_sd", 0.3),
        ("censored_fraction", 0.2),
        ("unscorable_fraction_of_uncensored", 0.2),
        ("independence_unit_icc", 0.2),
        ("task_icc", 0.2),
    ],
)
def test_more_variance_or_attrition_never_reduces_denominator(
    scenario: dict[str, Any], field: str, value: float
) -> None:
    baseline = power.plan_scenario(scenario)["required_episode_denominator"]
    changed = copy.deepcopy(scenario)
    changed[field] = value
    if field.endswith("icc"):
        changed[
            "independence_unit_cluster_sizes"
            if field == "independence_unit_icc"
            else "task_cluster_sizes"
        ] = [2, 4]

    assert power.plan_scenario(changed)["required_episode_denominator"] >= baseline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paired_loss_difference_sd", 0.0),
        ("minimum_relative_brier_improvement", 0.0),
        ("censored_fraction", 1.0),
        ("target_power", 1.0),
    ],
)
def test_invalid_assumptions_fail_closed(
    scenario: dict[str, Any], field: str, value: float
) -> None:
    scenario[field] = value

    with pytest.raises(contract.ContractViolation, match=field):
        power.plan_scenario(scenario)


def test_size_weighted_geometry_penalizes_concentration() -> None:
    assert power.size_weighted_mean_cluster_size([1, 1, 8]) > 3
    assert power.design_effect([1, 1, 8], 0.2) > 1


def test_checked_in_sensitivity_declaration_is_planning_only() -> None:
    declaration = contract.load_json(
        Path(
            "docs/evaluations/eisv-incremental-value/"
            "power-planning-assumptions-v1.example.json"
        )
    )

    report = power.build_report(declaration)

    assert len(report["scenarios"]) == 2
    assert all(row["status"] == "PLANNING_ONLY" for row in report["scenarios"])
    assert all(row["decision_authority"] == "NONE" for row in report["scenarios"])
