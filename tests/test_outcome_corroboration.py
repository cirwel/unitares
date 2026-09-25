import pytest

from src.outcome_corroboration import (
    NO_CEILING,
    SERVER_SET_TOOL_TRIGGERS,
    assess_outcome_corroboration,
    enrich_detail_with_corroboration,
    tool_observation_triggers,
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
        ceiling=NO_CEILING,
    )

    # Detector behaviour, with the cap explicitly lifted: a verified marker plus
    # a trusted source promotes the refs.
    assert verified.grade == "externally_verified"
    assert set(verified.verified_fields) == {"commit", "pr"}
    assert verified.unverified_fields == []

    # But this row is AGENT-attested, and the marker and source are both values
    # the agent wrote. Under the default it cannot self-promote. This assertion
    # is new: the case above previously ran uncapped and read as blessing it.
    capped = assess_outcome_corroboration(
        "task_completed",
        {
            "pr": 661,
            "commit_sha": "abc123",
            "evidence": [
                {"source": "github", "verified": True, "pr": 661, "commit_sha": "abc123"}
            ],
        },
        "agent_reported_tool_result",
    )
    assert capped.grade == "tool_observed"
    assert capped.verified_fields == []


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


def test_caller_substrate_vocabulary_alone_no_longer_earns_substrate():
    """The asymmetry is closed at the GRADER, not just at the boundary.

    _has_substrate_evidence used to match _TRUSTED_SUBSTRATE_MARKERS against
    _source_text with NO verified-marker requirement, while
    _has_external_evidence required both. Since `source` is not among the keys
    outcome_events._strip_provenance_claims removes, a bare agent claim reached
    0.85 -- above the 0.65 calibration gate -- on an assertion alone.

    The handler ceiling caps that at the public boundary. This asserts the
    weaker premise is gone outright, so a future surface that never learns
    about the ceiling still cannot buy substrate authority with a string.
    """
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "summary": "did the thing"},
        "agent_reported_tool_result",
    )

    assert assessment.grade == "claim_only"
    assert assessment.evidence_weight == 0.10


def test_substrate_and_external_are_now_gated_symmetrically():
    """Both paths require a verified marker AND a trusted source string."""
    marker_only = assess_outcome_corroboration(
        "task_completed", {"substrate_verified": True},
        "agent_reported_tool_result", ceiling=NO_CEILING,
    )
    source_only = assess_outcome_corroboration(
        "task_completed", {"source": "sensor_sync"},
        "agent_reported_tool_result", ceiling=NO_CEILING,
    )
    both = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "substrate_verified": True},
        "agent_reported_tool_result", ceiling=NO_CEILING,
    )

    assert marker_only.grade != "substrate_observed"
    assert source_only.grade != "substrate_observed"
    assert both.grade == "substrate_observed"


def test_server_controlled_provenance_still_short_circuits():
    """Tightening the free-text path must not disarm real server ingestion,
    which reaches its grade through verification_source, not through detail."""
    assert assess_outcome_corroboration(
        "trajectory_validated",
        {"source": "trajectory_self_validation"},
        "server_observation",
    ).grade == "substrate_observed"
    assert assess_outcome_corroboration(
        "task_completed", {"pr": 1}, "external_signal"
    ).grade == "externally_verified"


def test_ceiling_caps_caller_claimed_substrate():
    assessment = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "substrate_verified": True, "summary": "did it"},
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
        ceiling=NO_CEILING,
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
        {"source": "sensor_sync", "substrate_verified": True},
        outcome_type="task_completed",
        verification_source="agent_reported_tool_result",
        ceiling="tool_observed",
    )

    assert out["corroboration_grade"] == "tool_observed"
    assert out["evidence_weight"] == 0.65


def test_capping_also_downgrades_the_field_level_verdicts():
    """A capped row must not report its claims MORE verified than an honest one.

    The field verdicts are derived from the pre-cap evidence flags, so before
    this was fixed a capped row reported verified=['commit','pr'] while a
    genuinely tool_observed row reported those same fields UNVERIFIED.
    """
    capped = assess_outcome_corroboration(
        "task_completed",
        {"evidence": [{"source": "ci", "ci_verified": True, "pr": 42, "commit_sha": "abc"}]},
        "agent_reported_tool_result",
        ceiling="tool_observed",
    )
    honest = assess_outcome_corroboration(
        "task_completed",
        {"tool": "pytest", "kind": "test", "exit_code": 0, "pr": 42, "commit_sha": "abc"},
        "agent_reported_tool_result",
    )

    assert capped.grade == honest.grade == "tool_observed"
    # The claim fields the cap stripped authority for are no longer "verified".
    assert "pr" not in capped.verified_fields
    assert "commit" not in capped.verified_fields
    assert {"pr", "commit"} <= set(capped.unverified_fields)
    # And the honest row is not made to look worse than the capped one.
    assert {"pr", "commit"} <= set(honest.unverified_fields)


def test_capping_never_raises_a_verification_flag_it_did_not_earn():
    """Capping TO tool_observed must not assert a tool observation."""
    capped = assess_outcome_corroboration(
        "task_completed",
        {"source": "sensor_sync", "substrate_verified": True, "test_command": "pytest -q"},
        "agent_reported_tool_result",
        ceiling="tool_observed",
    )
    assert capped.grade == "tool_observed"
    assert capped.verified_fields == []


def test_ceiling_for_verification_source_caps_only_self_attested_rows():
    from src.outcome_corroboration import ceiling_for_verification_source

    assert ceiling_for_verification_source("agent_reported_tool_result") == "tool_observed"
    # A vouched provenance lifts the cap only as far as its OWN claim reaches.
    # These returned NO_CEILING until an external review pointed out that
    # vouching the transport also stopped clamping the payload it carried.
    assert ceiling_for_verification_source("external_signal") == "externally_verified"
    assert ceiling_for_verification_source("server_observation") == "substrate_observed"
    # NO_CEILING remains the explicit opt-out a CALLER can pass -- omission and
    # "trusted" must not be spelled the same way -- it is just no longer what
    # provenance alone hands out.
    assert NO_CEILING not in {
        ceiling_for_verification_source(s)
        for s in (None, "", "external_signal", "server_observation",
                  "agent_reported_tool_result")
    }
    # Unknown/NULL provenance now CAPS. It previously returned None (uncapped),
    # which let pre-column rows re-grade to 0.85 on the audit surface built to
    # expose self-labelled rows. Unknown is not evidence of verification.
    assert ceiling_for_verification_source(None) == "tool_observed"


def test_stored_self_attested_row_regrades_to_the_capped_value():
    """A read-side re-grade must reproduce what was recorded, not what the
    row's caller-supplied detail text can still claim."""
    from src.outcome_corroboration import ceiling_for_verification_source

    stored_detail = {
        "source": "sensor_sync",
        "substrate_verified": True,
        "summary": "did the thing",
    }
    source = "agent_reported_tool_result"

    uncapped = assess_outcome_corroboration(
        "task_completed", stored_detail, source, ceiling=NO_CEILING
    )
    regraded = assess_outcome_corroboration(
        "task_completed",
        stored_detail,
        source,
        ceiling=ceiling_for_verification_source(source),
    )

    assert uncapped.grade == "substrate_observed"   # what the audit used to report
    assert regraded.grade == "tool_observed"        # what was actually persisted


def test_nested_external_signal_needs_a_verified_marker():
    """Closes the last asymmetry, found by an external review (Codex).

    _has_external_evidence short-circuited on a NESTED verification_source with
    no verified marker, while _has_substrate_evidence directly below it required
    one. A nested verification_source is caller-authored dict content like any
    other key, so that was the same asymmetry this change set out to remove,
    still live one branch lower.
    """
    bare = assess_outcome_corroboration(
        "task_completed",
        {"evidence": [{"verification_source": "external_signal"}]},
        "agent_reported_tool_result",
        ceiling=NO_CEILING,
    )
    marked = assess_outcome_corroboration(
        "task_completed",
        {"evidence": [{"verification_source": "external_signal", "verified": True}]},
        "agent_reported_tool_result",
        ceiling=NO_CEILING,
    )

    assert bare.grade == "claim_only"
    assert marked.grade == "externally_verified"


def test_top_level_provenance_argument_still_short_circuits():
    """The ARGUMENT is set by server code, not by a payload, so it keeps its
    short-circuit. Only the nested caller-authored copy lost it."""
    assert assess_outcome_corroboration(
        "task_completed", {"pr": 1}, "external_signal"
    ).grade == "externally_verified"
    assert assess_outcome_corroboration(
        "trajectory_validated", {"source": "trajectory_self_validation"}, "server_observation"
    ).grade == "substrate_observed"


class TestToolObservationTriggerOrigin:
    """The 0.65 tier is the calibration admission threshold, so WHICH trigger
    fired is a governance fact, not a detail. These pin the two properties the
    provenance-split diagnostic reads:
    ``scripts/diagnostics/outcome_evidence_provenance_split.py``.
    """

    def test_trigger_enumeration_agrees_with_the_grading_verdict(self):
        """The enumerator is the boolean predicate's only implementation, so a
        future trigger added to one cannot go missing from the other."""
        cases = [
            {},
            {"summary": "did the thing"},
            {"phase5_emitter": True},
            {"kind": "test", "exit_code": 0},
            {"tool": "pytest", "returncode": 1},
            {"source": "recent_tool_results"},
            {"tool_results": [{"ok": True}]},
            {"captured_output": "..."},
            {"phase5_emitter": True, "kind": "command", "exit_code": 0},
        ]
        for detail in cases:
            fired = bool(tool_observation_triggers(detail))
            graded = assess_outcome_corroboration(
                "task_completed", detail, "agent_reported_tool_result"
            ).grade
            assert fired is (graded == "tool_observed"), detail

    def test_only_phase5_emitter_is_server_set(self):
        """Every other trigger is caller-authored vocabulary: an agent that
        DESCRIBES a tool call reaches the same grade as one the server watched.
        If a trigger becomes server-set, it belongs in this set — and if a new
        caller-authored one is added, this test says so out loud."""
        assert SERVER_SET_TOOL_TRIGGERS == {"phase5_emitter"}

        caller_authored = [
            {"kind": "test", "exit_code": 0},
            {"tool": "pytest", "exit_code": 0},
            {"source": "tool_result"},
            {"command_results": ["ok"]},
            {"observed_command": "pytest -q"},
        ]
        for detail in caller_authored:
            triggers = tool_observation_triggers(detail)
            assert triggers, detail
            assert not (triggers & SERVER_SET_TOOL_TRIGGERS), detail
            assert (
                assess_outcome_corroboration(
                    "task_completed", detail, "agent_reported_tool_result"
                ).evidence_weight
                == 0.65
            ), detail

    def test_a_clamped_row_can_sit_at_0_65_with_no_trigger_at_all(self):
        """The diagnostic's ``no_trigger_clamped`` bucket is a real state, not a
        parse failure: substrate evidence capped to the provenance ceiling lands
        at tool_observed without any tool trigger firing."""
        detail = {"source": "server_observation", "verified": True}
        assessment = assess_outcome_corroboration(
            "task_completed", detail, "agent_reported_tool_result"
        )
        assert assessment.grade == "tool_observed"
        assert tool_observation_triggers(detail) == set()


class TestVouchedProvenanceIsNotAWarrantForItsPayload:
    """Both findings from the external review of this PR's own diff
    (gpt-5.6-terra via the Codex host adapter, 2026-09-19). Each was
    reproduced against the pre-fix code before being fixed.
    """

    def test_an_unrecognised_ceiling_falls_back_instead_of_failing_open(self):
        """The clamp is guarded by ``ceiling in _GRADE_RANK``, so an
        unrecognised ceiling used to disable it SILENTLY. The nastiest spelling
        is the constant's own NAME: TOOL_OBSERVED == "tool_observed", so
        ``ceiling="TOOL_OBSERVED"`` looks right, reads right, and graded 0.85.
        Default-deny that fails open on a typo is not default-deny.
        """
        detail = {"source": "sensor_sync", "verified": True}
        for bogus in ("", "typo", "TOOL_OBSERVED", "none", "0.65"):
            assessment = assess_outcome_corroboration(
                "task_completed", detail, "agent_reported_tool_result", ceiling=bogus
            )
            assert assessment.grade == "tool_observed", bogus
            assert any("unrecognised ceiling" in r for r in assessment.reasons), bogus

        # The fallback is the PROVENANCE default, not a fixed constant, so a
        # typo on a vouched row degrades to what that row is entitled to
        # rather than all the way down.
        vouched = assess_outcome_corroboration(
            "trajectory_validated",
            {"source": "trajectory_self_validation"},
            "server_observation",
            ceiling="typo",
        )
        assert vouched.grade == "substrate_observed"

    def test_a_server_observation_row_cannot_be_talked_up_to_externally_verified(self):
        """Vouching the TRANSPORT is not vouching the TEXT it carries. A
        caller-authored nested marker reached 1.00 on a server_observation row,
        because NO_CEILING stopped the clamp entirely instead of holding the
        row to what its own provenance asserts.
        """
        payload = {"evidence": [{"verification_source": "external_signal", "verified": True}]}

        assert assess_outcome_corroboration(
            "task_completed", payload, "server_observation"
        ).grade == "substrate_observed"

        # Still reachable for a row whose own provenance says external.
        assert assess_outcome_corroboration(
            "task_completed", payload, "external_signal"
        ).grade == "externally_verified"

        # And an explicit NO_CEILING from a caller that has established trust
        # by other means is unchanged -- the opt-out still exists.
        assert assess_outcome_corroboration(
            "task_completed", payload, "server_observation", ceiling=NO_CEILING
        ).grade == "externally_verified"


# --- corroboration_upgrade_hint ------------------------------------------------
#
# An external agent told "task_completed completion claim has no corroborating
# detail" guessed test_exit_code / artifact_hash / external_verifier_id, none of
# which the grader reads (2026-09-24). The hint names the reference keys that do
# count, pinned here to the grader. It deliberately does NOT name the shapes that
# reach tool_observed: that grade is the calibration admission weight, and a
# caller can reach it by description alone (SERVER_SET_TOOL_TRIGGERS), which is
# an open operator decision the hint must not pre-empt.

from src.outcome_corroboration import (  # noqa: E402
    CLAIM_ONLY,
    GRADE_WEIGHTS,
    SELF_REPORT_WITH_REFS,
    SUBSTRATE_OBSERVED,
    TOOL_OBSERVED,
    _CLAIM_FIELD_FAMILIES,
    _REF_EXAMPLE_KEYS,
    corroboration_upgrade_hint,
)
from src.mcp_handlers.observability.outcome_events import (  # noqa: E402
    _MIN_TACTICAL_EVIDENCE_WEIGHT,
)


def _public_grade(detail):
    return assess_outcome_corroboration(
        "task_completed", detail, "agent_reported_tool_result", ceiling=TOOL_OBSERVED
    ).grade


def test_hint_advertises_every_reference_family_except_command():
    """command's keys (exit_code, returncode) are tool_observed triggers."""
    assert set(_REF_EXAMPLE_KEYS) == set(_CLAIM_FIELD_FAMILIES) - {"command"}
    for family, key in _REF_EXAMPLE_KEYS.items():
        assert key in _CLAIM_FIELD_FAMILIES[family]


@pytest.mark.parametrize("key", list(_REF_EXAMPLE_KEYS.values()))
@pytest.mark.parametrize(
    "context",
    [{}, {"tool": "pytest"}, {"kind": "build"}, {"kind": "test", "tool": "pytest"}],
)
def test_no_advertised_key_combines_into_a_tool_trigger(key, context):
    """Following the hint on top of a partial tool description must not cross
    the calibration floor (review of #2430)."""
    grade = _public_grade({**context, key: "x"})
    assert GRADE_WEIGHTS[grade] < _MIN_TACTICAL_EVIDENCE_WEIGHT


@pytest.mark.parametrize("key", list(_REF_EXAMPLE_KEYS.values()))
def test_every_advertised_reference_key_reaches_self_report_with_refs(key):
    assert _public_grade({key: "x"}) == SELF_REPORT_WITH_REFS


def test_what_the_hint_advertises_stays_below_the_calibration_floor():
    """The whole point of stopping at references."""
    assert GRADE_WEIGHTS[SELF_REPORT_WITH_REFS] < _MIN_TACTICAL_EVIDENCE_WEIGHT


def test_claim_only_hint_names_references_and_the_cap_but_no_tool_recipe():
    hint = corroboration_upgrade_hint(CLAIM_ONLY, ceiling=TOOL_OBSERVED)
    for key in _REF_EXAMPLE_KEYS.values():
        assert key in hint
    assert f"capped at {TOOL_OBSERVED}" in hint
    for recipe_word in ("exit_code", "returncode", "captured_output", "tool_results", "kind"):
        assert recipe_word not in hint


@pytest.mark.parametrize("grade", [SELF_REPORT_WITH_REFS, TOOL_OBSERVED])
def test_no_hint_above_claim_only(grade):
    assert corroboration_upgrade_hint(grade, ceiling=TOOL_OBSERVED) is None


@pytest.mark.parametrize("ceiling", [SUBSTRATE_OBSERVED, NO_CEILING, None, "bogus"])
def test_no_hint_off_the_self_attested_path(ceiling):
    """The cap sentence is only true for the public path; nobody else gets it."""
    assert corroboration_upgrade_hint(CLAIM_ONLY, ceiling=ceiling) is None


def test_the_guessed_keys_really_do_not_count():
    """The failure that motivated the hint, pinned so it stays documented."""
    assert _public_grade(
        {"test_exit_code": 0, "artifact_hash": "abc", "external_verifier_id": "v"}
    ) == CLAIM_ONLY
