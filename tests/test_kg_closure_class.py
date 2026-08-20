"""A closure must be able to say by WHAT STANDARD it closed.

`status` is two-valued over a three-valued world. A discovery is open or it is
closed, and the state a reconciler keeps meeting is neither — "not currently
observed, cause unknown". Forced to pick, everyone picks the one that shortens
the queue, and `status='resolved'` then records no standard at all.

The cost is not local. A reader cannot distinguish a closure resting on a
deployed fix with positively observed effect from one resting on a correlation
with a date, so a weak closure dilutes what "resolved" means graph-wide,
retroactively, including entries other agents closed rigorously.

The two required-evidence rules below are not generic diligence prompts. Each
encodes a specific way a closure went wrong on 2026-08-19:

  fix_verified requires `observed` because a closure claimed a verified fix on
  evidence that the old symptom was gone. The subject-keying half of the repair
  had never been exercised live.

  unobserved requires `instrument_check` because a closure concluded a condition
  had ceased from a sibling signal still arriving. Siblings share the sink, not
  the emitter.
"""

import pytest

from src.mcp_handlers.knowledge.handlers import (  # type: ignore
    CLOSURE_CLASSES,
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
    _validate_closure_class,
)


def _request(**overrides):
    fields = {
        "arguments": {"discovery_id": "d-1"},
        "discovery_id": "d-1",
        "status": None,
        "details": None,
        "resolution_note": None,
        "summary": None,
        "severity": None,
        "discovery_type": None,
        "tags": None,
        "superseded_by": None,
        "closure_class": None,
        "closure_evidence": None,
    }
    fields.update(overrides)
    return _KnowledgeUpdateRequest(**fields)


class TestNonBreaking:
    """A required field would break the KG gardener's mechanical auto-resolve."""

    def test_absent_class_is_accepted(self):
        _validate_closure_class(_request(), "resolved")

    def test_absent_class_is_accepted_on_every_closing_status(self):
        for status in ("resolved", "closed", "wont_fix", "superseded"):
            _validate_closure_class(_request(), status)


class TestVocabulary:
    def test_unknown_class_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="probably_fine"), "resolved")

    @pytest.mark.parametrize(
        "cls", sorted(CLOSURE_CLASSES - {"fix_verified", "unobserved"})
    )
    def test_classes_without_required_evidence_pass_bare(self, cls):
        _validate_closure_class(_request(closure_class=cls), "resolved")

    def test_a_standard_cannot_be_declared_for_a_closure_that_is_not_happening(self):
        """closure_class on status='open' is a contradiction, not a hint."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="obsolete"), "open")


class TestFixVerifiedRequiresPositiveObservation:
    """The failure: claiming a verified fix because the old symptom is gone."""

    def test_bare_fix_verified_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="fix_verified"), "resolved")

    def test_deployed_without_observed_is_refused(self):
        """A merged-and-deployed PR is half the claim. It is not the effect."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence={"deployed": "dd8c0d74 is in build 377c8687"},
                ),
                "resolved",
            )

    def test_both_keys_pass(self):
        _validate_closure_class(
            _request(
                closure_class="fix_verified",
                closure_evidence={
                    "deployed": "dd8c0d74 ancestor of running build_sha 377c8687",
                    "observed": "new fingerprint tracks the event-type set across repeats",
                },
            ),
            "resolved",
        )

    def test_whitespace_is_not_evidence(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence={"deployed": "x", "observed": "   "},
                ),
                "resolved",
            )

    def test_evidence_must_be_an_object(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence="deployed and observed, trust me",
                ),
                "resolved",
            )


class TestUnobservedRequiresAnInstrumentCheck:
    """The failure: concluding a condition ceased from a sibling still arriving."""

    def test_bare_unobserved_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="unobserved"), "resolved")

    def test_window_alone_is_refused(self):
        """A date range with no liveness check is the exact 2026-08-19 error."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="unobserved",
                    closure_evidence={"window": "zero rows since week of 2026-07-06"},
                ),
                "resolved",
            )

    def test_both_keys_pass(self):
        _validate_closure_class(
            _request(
                closure_class="unobserved",
                closure_evidence={
                    "window": "zero rows since week of 2026-07-06",
                    "instrument_check": (
                        "total failing-row volume dropped with it rather than "
                        "staying flat, so the rows were not relabelled"
                    ),
                },
            ),
            "resolved",
        )


class TestTheHintNamesTheTrap:
    """The recovery text has to teach the distinction, not just list keys."""

    def _hint(self, cls, status="resolved"):
        try:
            _validate_closure_class(_request(closure_class=cls), status)
        except _UpdateResponseError as exc:
            return str(exc.response.text)
        pytest.fail("expected a refusal")

    def test_fix_verified_hint_rejects_absence_as_observation(self):
        assert "not an observation" in self._hint("fix_verified")

    def test_unobserved_hint_names_the_sibling_fallacy(self):
        hint = self._hint("unobserved")
        assert "sink" in hint and "emitter" in hint
