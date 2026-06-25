"""Watcher resolution → external-truth outcome_event (the first exogenous
ground-truth channel for an EISV-bearing resident; roadmap Appendix B).

Confirmed = Watcher RIGHT (good). Only a 'fp' dismissal = Watcher WRONG (bad);
other dismissals (out_of_scope/wont_fix/…) drop a VALID finding, so Watcher was
still right. Adjudication is outside the loop → verification_source must be
'external_signal' so it is NOT excluded by Invariant 4. The outcome_type must be
in the handler's VALID_OUTCOME_TYPES allowlist or the emit is rejected.
"""
import pytest

from agents.watcher.agent import build_resolution_outcome_args


def test_confirmed_is_good_external_truth():
    a = build_resolution_outcome_args("confirmed", "abc123", "watcher-uuid", reason="real bug")
    assert a["agent_id"] == "watcher-uuid"          # attribute to Watcher → EISV snapshot is Watcher's
    assert a["outcome_type"] == "watcher_finding_confirmed"
    assert a["is_bad"] is False                      # Watcher's judgment held
    assert a["verification_source"] == "external_signal"
    assert a["detail"]["fingerprint"] == "abc123"
    assert a["detail"]["resolution"] == "confirmed"
    assert a["detail"]["reason"] == "real bug"


def test_fp_dismissal_is_bad():
    a = build_resolution_outcome_args("dismissed", "def456", "watcher-uuid", reason="fp")
    assert a["outcome_type"] == "watcher_finding_dismissed"
    assert a["is_bad"] is True                       # false positive → Watcher was wrong


@pytest.mark.parametrize("reason", ["out_of_scope", "wont_fix", "dup", "unclear", "stale", "", None])
def test_non_fp_dismissal_is_not_bad(reason):
    """A valid-but-unactioned finding is NOT a bad outcome — only 'fp' counts."""
    a = build_resolution_outcome_args("dismissed", "x", "u", reason=reason)
    assert a["is_bad"] is False


def test_outcome_types_are_whitelisted():
    """Both emitted types must be in the handler's VALID_OUTCOME_TYPES, or the
    outcome_event tool rejects them (the #1061 bug: they weren't)."""
    from src.mcp_handlers.observability.outcome_events import VALID_OUTCOME_TYPES
    for status in ("confirmed", "dismissed"):
        a = build_resolution_outcome_args(status, "x", "u", reason="fp")
        assert a["outcome_type"] in VALID_OUTCOME_TYPES


def test_verification_source_is_anchorable():
    """The emitted source must pass the Stage-0 anchor filter, not be excluded.

    (outcome_anchors ships on the Stage-0 anchor-tiering branch; skip until merged.)
    """
    import pytest
    anchors = pytest.importorskip("src.grounding.outcome_anchors")
    for status in ("confirmed", "dismissed"):
        a = build_resolution_outcome_args(status, "fp", "u")
        assert anchors.is_exogenous_anchor(a["verification_source"]) is True
