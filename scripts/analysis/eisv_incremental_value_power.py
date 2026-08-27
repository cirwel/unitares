#!/usr/bin/env python3
"""Plan EISV A2-vs-A3 sample size from declared pilot assumptions.

This is an analytic planning estimate, not the confirmatory power freeze. It does
not read a database or episode store. The paired-loss standard deviation must
already include the A2/A3 covariance, cluster geometry is preserved through
the supplied size distributions, and censored/unscorable episodes inflate the
total denominator instead of disappearing from it.

Before confirmation, replace this bound with a registered two-way clustered
Monte Carlo analysis on authorized pilot estimates. Until then the result is
``PLANNING_ONLY`` and has no decision authority.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_incremental_value_contract import ContractViolation, load_json


SCHEMA_VERSION = "eisv-ablation-power-planning.v1"
STATUS = "PLANNING_ONLY"
REQUIRED_FIELDS = frozenset(
    {
        "scenario_id",
        "alpha_two_sided",
        "target_power",
        "baseline_brier",
        "minimum_relative_brier_improvement",
        "paired_loss_difference_sd",
        "censored_fraction",
        "unscorable_fraction_of_uncensored",
        "primary_adverse_rate",
        "independence_unit_icc",
        "task_icc",
        "independence_unit_cluster_sizes",
        "task_cluster_sizes",
    }
)


def _number(
    scenario: Mapping[str, Any],
    field: str,
    *,
    lower: float,
    upper: float,
    lower_open: bool = False,
    upper_open: bool = False,
) -> float:
    value = scenario.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContractViolation(f"{field} must be finite")
    lower_bad = result <= lower if lower_open else result < lower
    upper_bad = result >= upper if upper_open else result > upper
    if lower_bad or upper_bad:
        brackets = ("(" if lower_open else "[") + f"{lower}, {upper}" + (
            ")" if upper_open else "]"
        )
        raise ContractViolation(f"{field} must be in {brackets}")
    return result


def _cluster_sizes(scenario: Mapping[str, Any], field: str) -> list[int]:
    values = scenario.get(field)
    if not isinstance(values, list) or not values:
        raise ContractViolation(f"{field} must be a non-empty array")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ContractViolation(f"{field} must contain positive integers")
    return list(values)


def size_weighted_mean_cluster_size(sizes: Sequence[int]) -> float:
    """Return the cluster size experienced by a randomly selected episode."""

    if not sizes or any(isinstance(size, bool) or not isinstance(size, int) or size <= 0 for size in sizes):
        raise ContractViolation("cluster sizes must be positive integers")
    return sum(size * size for size in sizes) / sum(sizes)


def design_effect(sizes: Sequence[int], icc: float) -> float:
    if not 0.0 <= icc <= 1.0 or not math.isfinite(icc):
        raise ContractViolation("ICC must be finite and in [0, 1]")
    mean_size = size_weighted_mean_cluster_size(sizes)
    return 1.0 + (mean_size - 1.0) * icc


def validate_scenario(scenario: Mapping[str, Any]) -> None:
    missing = sorted(REQUIRED_FIELDS.difference(scenario))
    extra = sorted(set(scenario).difference(REQUIRED_FIELDS))
    if missing or extra:
        raise ContractViolation(f"scenario fields differ: missing={missing}, extra={extra}")
    scenario_id = scenario["scenario_id"]
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ContractViolation("scenario_id must be a non-empty string")
    _number(scenario, "alpha_two_sided", lower=0.0, upper=1.0, lower_open=True, upper_open=True)
    _number(scenario, "target_power", lower=0.5, upper=1.0, upper_open=True)
    _number(scenario, "baseline_brier", lower=0.0, upper=1.0, lower_open=True)
    _number(
        scenario,
        "minimum_relative_brier_improvement",
        lower=0.0,
        upper=1.0,
        lower_open=True,
    )
    _number(scenario, "paired_loss_difference_sd", lower=0.0, upper=1.0, lower_open=True)
    _number(scenario, "censored_fraction", lower=0.0, upper=1.0, upper_open=True)
    _number(
        scenario,
        "unscorable_fraction_of_uncensored",
        lower=0.0,
        upper=1.0,
        upper_open=True,
    )
    _number(scenario, "primary_adverse_rate", lower=0.0, upper=1.0)
    _number(scenario, "independence_unit_icc", lower=0.0, upper=1.0)
    _number(scenario, "task_icc", lower=0.0, upper=1.0)
    _cluster_sizes(scenario, "independence_unit_cluster_sizes")
    _cluster_sizes(scenario, "task_cluster_sizes")


def plan_scenario(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate one transparent analytic planning scenario."""

    validate_scenario(scenario)
    alpha = float(scenario["alpha_two_sided"])
    power = float(scenario["target_power"])
    baseline_brier = float(scenario["baseline_brier"])
    relative_improvement = float(scenario["minimum_relative_brier_improvement"])
    paired_sd = float(scenario["paired_loss_difference_sd"])
    absolute_improvement = baseline_brier * relative_improvement
    normal = statistics.NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha / 2.0)
    z_power = normal.inv_cdf(power)
    iid_episodes = ((z_alpha + z_power) * paired_sd / absolute_improvement) ** 2

    unit_sizes = _cluster_sizes(scenario, "independence_unit_cluster_sizes")
    task_sizes = _cluster_sizes(scenario, "task_cluster_sizes")
    unit_effect = design_effect(unit_sizes, float(scenario["independence_unit_icc"]))
    task_effect = design_effect(task_sizes, float(scenario["task_icc"]))
    # Product is an explicit inflation heuristic intended for sensitivity
    # planning. It is not a proven bound or a substitute for the required
    # two-way clustered Monte Carlo power analysis.
    combined_effect = unit_effect * task_effect
    usable_fraction = (1.0 - float(scenario["censored_fraction"])) * (
        1.0 - float(scenario["unscorable_fraction_of_uncensored"])
    )
    required_denominator = math.ceil(iid_episodes * combined_effect / usable_fraction)
    expected_adverse = required_denominator * float(scenario["primary_adverse_rate"])
    return {
        "scenario_id": scenario["scenario_id"],
        "status": STATUS,
        "decision_authority": "NONE",
        "estimand": "paired_episode_brier_loss_a3_minus_a2",
        "absolute_brier_improvement": absolute_improvement,
        "paired_loss_difference_sd": paired_sd,
        "paired_covariance_requirement": "EMBODIED_IN_DECLARED_SD",
        "iid_usable_episode_bound": iid_episodes,
        "independence_unit_size_weighted_mean": size_weighted_mean_cluster_size(unit_sizes),
        "task_size_weighted_mean": size_weighted_mean_cluster_size(task_sizes),
        "independence_unit_design_effect": unit_effect,
        "task_design_effect": task_effect,
        "combined_design_effect": combined_effect,
        "usable_fraction": usable_fraction,
        "required_episode_denominator": required_denominator,
        "expected_primary_adverse_outcomes": expected_adverse,
        "denominator_rule": "CENSORED_AND_UNSCORABLE_RETAINED",
        "required_next_step": "REGISTERED_TWO_WAY_CLUSTERED_MONTE_CARLO",
    }


def build_report(declaration: Mapping[str, Any]) -> dict[str, Any]:
    if declaration.get("schema_version") != SCHEMA_VERSION:
        raise ContractViolation(f"schema_version must be {SCHEMA_VERSION}")
    if declaration.get("status") != STATUS:
        raise ContractViolation(f"input status must be {STATUS}")
    if set(declaration).difference({"schema_version", "status", "notes", "scenarios"}):
        raise ContractViolation("power declaration contains unknown top-level fields")
    scenarios = declaration.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ContractViolation("scenarios must be a non-empty array")
    if not all(isinstance(scenario, Mapping) for scenario in scenarios):
        raise ContractViolation("every scenario must be an object")
    ids = [scenario.get("scenario_id") for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ContractViolation("scenario_id values must be unique")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "decision_authority": "NONE",
        "notes": declaration.get("notes"),
        "scenarios": [plan_scenario(scenario) for scenario in scenarios],
        "limitations": [
            "No pilot or production episode data was read.",
            "The product of one-way design effects is a sensitivity heuristic, not a proven bound or final two-way clustered model.",
            "The paired-loss standard deviation must be estimated under separately authorized pilot score/outcome access before confirmation.",
            "Expected adverse outcomes are not a frozen adverse-outcome stopping requirement.",
        ],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assumptions", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(load_json(args.assumptions))
    except ContractViolation as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
