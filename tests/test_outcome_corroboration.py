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


def test_caller_substrate_vocabulary_reaches_substrate_observed_ungated():
    """Pins the hole the ceiling exists to close.

    _has_substrate_evidence matches _TRUSTED_SUBSTRATE_MARKERS against
    _source_text with NO verified-marker requirement -- unlike
    _has_external_evidence, which requires both. `source` is not among the keys
    outcome_events._strip_provenance_claims removes, so a bare agent claim
    reached 0.85, above the 0.65 calibration gate.
    """
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "summary": "did the thing"},
        "agent_reported_tool_result",
    )

    assert assessment.grade == "substrate_observed"
    assert assessment.evidence_weight == 0.85


def test_ceiling_caps_caller_claimed_substrate():
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "summary": "did the thing"},
        "agent_reported_tool_result",
        ceiling="tool_observed",
    )

    assert assessment.grade == "tool_observed"
    assert assessment.evidence_weight == 0.65
    assert any("capped at tool_observed" in r for r in assessment.reasons)


def test_ceiling_caps_caller_claimed_external_verification():
    """The external path needs a marker AND a source string, but both are
    caller-supplied on the public path, so it is claimable with two keys."""
    uncapped = assess_outcome_corroboration(
        "task_completed",
        {"evidence": [{"source": "ci", "ci_verified": True, "pr": 42}]},
        "agent_reported_tool_result",
    )
    assert uncapped.grade == "externally_verified"
    assert uncapped.evidence_weight == 1.00

    capped = assess_outcome_corroboration(
        "task_completed",
        {"evidence": [{"source": "ci", "ci_verified": True, "pr": 42}]},
        "agent_reported_tool_result",
        ceiling="tool_observed",
    )
    assert capped.grade == "tool_observed"
    assert capped.evidence_weight == 0.65


def test_ceiling_never_promotes_a_weaker_grade():
    """A ceiling is a maximum, not an assignment: a claim-only row must not be
    lifted to the ceiling."""
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"summary": "trust me"},
        "agent_reported_tool_result",
        ceiling="tool_observed",
    )

    assert assessment.grade == "claim_only"
    assert assessment.evidence_weight == 0.10


def test_ceiling_unset_or_unrecognised_is_a_noop():
    """Server-controlled ingestion passes no ceiling and keeps the full range;
    an unrecognised value must not silently clamp to the floor."""
    for ceiling in (None, "not_a_grade"):
        assessment = assess_outcome_corroboration(
            "trajectory_validated",
            {"source": "trajectory_self_validation"},
            "server_observation",
            ceiling=ceiling,
        )
        assert assessment.grade == "substrate_observed"


def test_enrich_threads_the_ceiling_into_persisted_metadata():
    out = enrich_detail_with_corroboration(
        {"source": "sensor_sync"},
        outcome_type="task_completed",
        verification_source="agent_reported_tool_result",
        ceiling="tool_observed",
    )

    assert out["corroboration_grade"] == "tool_observed"
    assert out["evidence_weight"] == 0.65
