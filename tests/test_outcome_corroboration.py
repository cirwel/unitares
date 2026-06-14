from src.outcome_corroboration import (
    assess_outcome_corroboration,
    enrich_detail_with_corroboration,
)


def test_agent_reported_task_completed_summary_only_is_claim_only():
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"summary": "Implemented and shipped a complete fix."},
        "agent_reported_tool_result",
    )

    assert assessment.grade == "claim_only"
    assert assessment.evidence_weight == 0.10
    assert assessment.claim_risk == "high"


def test_tool_result_test_passed_is_tool_observed():
    assessment = assess_outcome_corroboration(
        "test_passed",
        {
            "phase5_emitter": True,
            "kind": "test",
            "tool": "pytest",
            "exit_code": 0,
            "summary": "1 passed",
        },
        "agent_reported_tool_result",
    )

    assert assessment.grade == "tool_observed"
    assert assessment.evidence_weight > 0.5
    assert "test" in assessment.claimed_fields


def test_server_observation_trajectory_is_substrate_observed():
    assessment = assess_outcome_corroboration(
        "trajectory_validated",
        {
            "source": "trajectory_self_validation",
            "prev_norm": 0.1,
            "current_norm": 0.11,
        },
        "server_observation",
    )

    assert assessment.grade == "substrate_observed"
    assert assessment.claim_risk == "low"


def test_pr_commit_refs_are_untrusted_until_verified():
    unverified = assess_outcome_corroboration(
        "task_completed",
        {"pr": 661, "commit_sha": "abc123", "summary": "PR merged"},
        "agent_reported_tool_result",
    )

    assert unverified.grade == "self_report_with_refs"
    assert set(unverified.claimed_fields) == {"commit", "pr"}
    assert set(unverified.unverified_fields) == {"commit", "pr"}
    assert unverified.verified_fields == []

    verified = assess_outcome_corroboration(
        "task_completed",
        {
            "pr": 661,
            "commit_sha": "abc123",
            "evidence": [
                {
                    "source": "github",
                    "verified": True,
                    "pr": 661,
                    "commit_sha": "abc123",
                }
            ],
        },
        "agent_reported_tool_result",
    )

    assert verified.grade == "externally_verified"
    assert set(verified.verified_fields) == {"commit", "pr"}
    assert verified.unverified_fields == []


def test_bare_verified_flag_without_source_does_not_upgrade_refs():
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"pr": 661, "externally_verified": True},
        "agent_reported_tool_result",
    )

    assert assessment.grade == "self_report_with_refs"
    assert assessment.verified_fields == []
    assert assessment.unverified_fields == ["pr"]


def test_unset_verification_source_degrades_conservatively():
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"summary": "Done"},
        None,
    )

    assert assessment.grade == "claim_only"
    assert "verification_source unset" in " ".join(assessment.reasons)


def test_enrich_detail_adds_additive_metadata():
    detail = enrich_detail_with_corroboration(
        {"summary": "Done"},
        outcome_type="task_completed",
        verification_source="agent_reported_tool_result",
    )

    assert detail["corroboration_grade"] == "claim_only"
    assert detail["evidence_weight"] == 0.10
    assert detail["claim_risk"] == "high"
