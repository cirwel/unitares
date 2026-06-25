"""Watcher resolution → external-truth outcome_event (the first exogenous
ground-truth channel for an EISV-bearing resident; roadmap Appendix B).

Confirmed finding = Watcher was RIGHT (good); dismissed = false positive, Watcher
was WRONG (bad). Adjudication is outside the loop → verification_source must be
'external_signal' so it is NOT excluded by Invariant 4.
"""
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


def test_dismissed_is_bad_external_truth():
    a = build_resolution_outcome_args("dismissed", "def456", "watcher-uuid")
    assert a["outcome_type"] == "watcher_finding_dismissed"
    assert a["is_bad"] is True                       # false positive → Watcher was wrong
    assert a["verification_source"] == "external_signal"
    assert a["detail"]["reason"] == ""               # optional


def test_verification_source_is_anchorable():
    """The emitted source must pass the Stage-0 anchor filter, not be excluded.

    (outcome_anchors ships on the Stage-0 anchor-tiering branch; skip until merged.)
    """
    import pytest
    anchors = pytest.importorskip("src.grounding.outcome_anchors")
    for status in ("confirmed", "dismissed"):
        a = build_resolution_outcome_args(status, "fp", "u")
        assert anchors.is_exogenous_anchor(a["verification_source"]) is True
