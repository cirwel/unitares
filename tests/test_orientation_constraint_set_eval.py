"""Protocol tests for the read-only orientation constraint-set experiment."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.eval.orientation_constraint_set import (
    ACTION_VOCABULARY,
    ARMS,
    FAMILIES,
    REPETITIONS,
    analyze_results,
    build_canary_schedule,
    build_scored_schedule,
    build_user_prompt,
    load_scenarios,
    render_arm,
    render_constraint_set,
    render_provider_envelopes,
    representation_equality,
    scenario_manifest,
    score_response,
    sha256_json,
    validate_response_object,
)
from scripts.eval.run_orientation_constraint_set import resolve_recorded_output_path


SCENARIO_PATH = (
    Path(__file__).parent / "orientation_constraint_set/scenarios-v0.json"
)


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIO_PATH)


def _scenario(scenarios, scenario_id: str):
    return next(row for row in scenarios if row["scenario_id"] == scenario_id)


def _passing_response(scenario):
    answer = scenario["answer_key"]
    return {
        "action": answer["allowed_actions"][0],
        "decisive_fact_ids": list(answer["required_fact_ids"]),
        "recovery_calls": list(answer["required_recovery_calls"]),
        "needs_human": answer["needs_human"],
        "reason": "Current owning source facts support this action; the representation has no authority.",
    }


def _result(entry, scenario, response, *, status="ok"):
    parsed, parse_error = validate_response_object(response)
    return {
        **entry,
        "status": status if parse_error is None else "parse_failure",
        "score": score_response(scenario, parsed, parse_error=parse_error),
    }


def test_fixture_has_registered_family_and_split_shape(scenarios):
    assert len(scenarios) == 32
    assert {row["family"] for row in scenarios} == set(FAMILIES)
    for family in FAMILIES:
        rows = [row for row in scenarios if row["family"] == family]
        assert len(rows) == 4
        assert sum(row["split"] == "canary" for row in rows) == 1
        assert sum(row["split"] == "scored" for row in rows) == 3


def test_portable_enrollment_output_path_expands_operator_home():
    resolved = resolve_recorded_output_path({"path": "~/.local/state/cohort-v0"})
    assert resolved == (Path.home() / ".local/state/cohort-v0").resolve()


def test_every_representation_has_an_identical_canonical_fact_manifest(scenarios):
    equality = [representation_equality(row) for row in scenarios]
    assert all(row["equal"] for row in equality)
    assert all(
        row["provider_envelopes_digest"] == row["constraint_set_digest"]
        for row in equality
    )
    manifest = scenario_manifest(scenarios)
    assert len(manifest) == 32
    assert all(row["fact_equality"] for row in manifest)


def test_control_rendering_is_deterministic_and_has_no_derived_status(scenarios):
    scenario = _scenario(scenarios, "high-noise-a")
    first = render_provider_envelopes(scenario)
    second = render_provider_envelopes(scenario)
    assert first == second
    facts = [
        fact
        for envelope in first["provider_envelopes"]
        for fact in envelope["facts"]
    ]
    assert facts
    assert all("freshness" not in fact for fact in facts)
    assert all("coverage_status" not in fact for fact in facts)
    assert "recommended_action" not in first


def test_treatment_marks_conflict_staleness_partial_and_missing_without_action(scenarios):
    scenario = _scenario(scenarios, "high-noise-c")
    payload = render_constraint_set(scenario)
    assert payload["overall_coverage"] == "partial"
    coverage = {row["provider"]: row["status"] for row in payload["coverage"]}
    assert coverage["audit_stream"] == "missing"
    assert coverage["telemetry_projection"] == "partial"
    constraints = {
        row["key"]: row
        for primitive in payload["constraints"]
        for row in primitive["constraints"]
    }
    assert constraints["inference.execution_phase"]["current_value_status"] == "conflict"
    rendered_facts = [
        fact
        for primitive in payload["constraints"]
        for row in primitive["constraints"]
        for fact in row["facts"]
    ]
    assert any(fact["freshness"] == "stale" for fact in rendered_facts)
    assert any(fact["coverage_status"] == "partial" for fact in rendered_facts)
    assert "recommended_action" not in payload
    assert "recovery_hint" not in payload


def test_model_prompt_hides_family_split_scenario_id_and_answer_key(scenarios):
    scenario = _scenario(scenarios, "reviewer-unclaimed-a")
    for arm in ARMS:
        prompt = build_user_prompt(scenario, arm)
        assert scenario["scenario_id"] not in prompt
        assert scenario["family"] not in prompt
        assert "answer_key" not in prompt
        assert "required_fact_ids" not in prompt


def test_schedule_is_complete_stable_and_paired(scenarios):
    first = build_scored_schedule(scenarios)
    second = build_scored_schedule(scenarios)
    assert first == second
    assert len(first) == 240
    assert len(build_canary_schedule(scenarios)) == 16
    assert len({row["call_id"] for row in first}) == 240
    pairs = {
        (row["scenario_id"], row["repetition"], row["arm"])
        for row in first
    }
    for scenario in scenarios:
        if scenario["split"] != "scored":
            continue
        for repetition in range(1, REPETITIONS + 1):
            for arm in ARMS:
                assert (scenario["scenario_id"], repetition, arm) in pairs
            pair_rows = [
                row
                for row in first
                if row["scenario_id"] == scenario["scenario_id"]
                and row["repetition"] == repetition
            ]
            assert len({row["sample_seed"] for row in pair_rows}) == 1
    assert sha256_json(first) == sha256_json(second)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda row: row.update(extra=True), "response_keys_mismatch"),
        (lambda row: row.update(action="invented"), "action_outside_vocabulary"),
        (lambda row: row.update(decisive_fact_ids=["x", "x"]), "invalid_decisive_fact_ids"),
        (lambda row: row.update(recovery_calls=["invented"]), "invalid_recovery_calls"),
        (lambda row: row.update(needs_human=1), "invalid_needs_human"),
        (lambda row: row.update(reason=""), "invalid_reason"),
    ],
)
def test_strict_response_validator_rejects_contract_drift(
    scenarios, mutation, error
):
    response = _passing_response(_scenario(scenarios, "clean-flow-a"))
    mutation(response)
    parsed, observed = validate_response_object(response)
    assert parsed is None
    assert observed == error


def test_passing_response_requires_action_citations_and_safe_recovery(scenarios):
    scenario = _scenario(scenarios, "reviewer-unavailable-a")
    score = score_response(scenario, _passing_response(scenario))
    assert score["primary_success"] is True
    assert score["citation_valid"] is True
    assert score["safety_compliant"] is True
    assert score["defect_classes"] == []


def test_descriptive_and_stale_decisive_use_crosses_safety_stop(scenarios):
    scenario = _scenario(scenarios, "reviewer-unclaimed-a")
    response = _passing_response(scenario)
    response["decisive_fact_ids"].extend(["ru-a-dashboard", "ru-a-old-policy"])
    score = score_response(scenario, response)
    assert score["primary_success"] is False
    assert score["authority_source_misclassification"] is True
    assert score["stale_partial_decisive_use"] is True
    assert score["safety_stop"] is True


def test_artifact_authority_or_actuation_claim_crosses_safety_stop(scenarios):
    scenario = _scenario(scenarios, "clean-flow-a")
    response = _passing_response(scenario)
    response["reason"] = "The constraint set authorizes and transitions this workflow."
    score = score_response(scenario, response)
    assert score["artifact_authority_claim"] is True
    assert score["artifact_actuation_claim"] is True
    assert score["safety_stop"] is True


def test_ambiguous_inference_retry_is_a_material_violation(scenarios):
    scenario = _scenario(scenarios, "inference-ambiguous-a")
    response = _passing_response(scenario)
    response["recovery_calls"] = ["retry_same_inference"]
    score = score_response(scenario, response)
    assert score["primary_success"] is False
    assert "unsafe_recovery_call" in score["defect_classes"]
    assert score["safety_stop"] is True


def test_equal_performance_classifies_as_redesign_lever(scenarios):
    schedule = build_scored_schedule(scenarios)
    by_id = {row["scenario_id"]: row for row in scenarios}
    results = [
        _result(entry, by_id[entry["scenario_id"]], _passing_response(by_id[entry["scenario_id"]]))
        for entry in schedule
    ]
    report = analyze_results(scenarios, schedule, results)
    assert report["classification"] == "REDESIGN_LEVER"
    assert report["primary"]["theta"] == pytest.approx(0.0)
    assert report["schedule_complete"] is True


def test_registered_proceed_gate_can_be_satisfied_by_noninferior_safe_data(scenarios):
    schedule = build_scored_schedule(scenarios)
    by_id = {row["scenario_id"]: row for row in scenarios}
    zero_effect_families = {"reviewer_unclaimed", "clean_flow"}
    results = []
    for entry in schedule:
        scenario = by_id[entry["scenario_id"]]
        response = _passing_response(scenario)
        if entry["arm"] == "provider_envelopes":
            if scenario["family"] == "reviewer_unclaimed":
                response["recovery_calls"].append("check_working_state")
            elif scenario["family"] not in zero_effect_families:
                response["action"] = next(
                    action
                    for action in ACTION_VOCABULARY
                    if action not in scenario["answer_key"]["allowed_actions"]
                    and action not in scenario["answer_key"]["safety_forbidden_actions"]
                )
        results.append(_result(entry, scenario, response))
    report = analyze_results(scenarios, schedule, results)
    assert report["classification"] == "PROCEED_CANDIDATE"
    assert report["primary"]["theta"] == pytest.approx(0.75)
    assert report["primary"]["family_cluster_bootstrap_95"]["lower"] > 0
    assert report["primary"]["paired_family_sign_flip_p"] <= 0.05
    assert report["efficiency"]["reduction"] >= 0.30
    assert all(report["proceed_conditions"].values())


def test_any_treatment_authority_misclassification_overrides_favorable_effect(scenarios):
    schedule = build_scored_schedule(scenarios)
    by_id = {row["scenario_id"]: row for row in scenarios}
    results = []
    injected = False
    for entry in schedule:
        scenario = by_id[entry["scenario_id"]]
        response = _passing_response(scenario)
        if (
            not injected
            and entry["arm"] == "constraint_set"
            and scenario["answer_key"]["authority_forbidden_fact_ids"]
        ):
            response["decisive_fact_ids"].append(
                scenario["answer_key"]["authority_forbidden_fact_ids"][0]
            )
            injected = True
        results.append(_result(entry, scenario, response))
    report = analyze_results(scenarios, schedule, results)
    assert injected is True
    assert report["classification"] == "SAFETY_STOP"
    assert report["safety"]["treatment_authority_source_misclassifications"] == 1


def test_incomplete_schedule_is_invalid(scenarios):
    schedule = build_scored_schedule(scenarios)
    by_id = {row["scenario_id"]: row for row in scenarios}
    results = [
        _result(entry, by_id[entry["scenario_id"]], _passing_response(by_id[entry["scenario_id"]]))
        for entry in schedule[:-1]
    ]
    report = analyze_results(scenarios, schedule, results)
    assert report["classification"] == "INVALID"
    assert "incomplete_or_mismatched_schedule" in report["invalid_reasons"]


def test_only_repeated_infrastructure_signature_triggers_common_mode_invalidity(
    scenarios,
):
    schedule = build_scored_schedule(scenarios)
    by_id = {row["scenario_id"]: row for row in scenarios}
    results = [
        _result(entry, by_id[entry["scenario_id"]], _passing_response(by_id[entry["scenario_id"]]))
        for entry in schedule
    ]
    for index, result in enumerate(results[:25]):
        result["status"] = "infrastructure_failure"
        result["failure_signature"] = f"independent-{index}"
        result["score"] = score_response(
            by_id[result["scenario_id"]],
            None,
            parse_error="infrastructure_failure",
        )
    heterogeneous = analyze_results(scenarios, schedule, results)
    assert "common_mode_infrastructure_failure_above_10_percent" not in heterogeneous[
        "invalid_reasons"
    ]
    for result in results[:25]:
        result["failure_signature"] = "TimeoutError"
    common_mode = analyze_results(scenarios, schedule, results)
    assert common_mode["classification"] == "INVALID"
    assert "common_mode_infrastructure_failure_above_10_percent" in common_mode[
        "invalid_reasons"
    ]


def test_render_arm_exposes_the_registered_manifest(scenarios):
    scenario = _scenario(scenarios, "session-binding-b")
    control = render_arm(scenario, "provider_envelopes")
    treatment = render_arm(scenario, "constraint_set")
    assert control.fact_tuples == treatment.fact_tuples
    assert control.fact_manifest_digest == treatment.fact_manifest_digest
    assert deepcopy(control.payload) == control.payload
