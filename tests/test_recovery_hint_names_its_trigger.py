"""The recovery hint must describe the condition that actually fired.

2026-08-20. `_needs_attention` returns True for EITHER an advisory verdict
(guide/pause/reject) OR a near-edge margin -- but both paths returned the
margin-worded hint. So an agent with a COMFORTABLE margin and a `guide` verdict
was told "Policy margin is near an edge": a threshold claim standing in for a
verdict condition, and unfalsifiable from the agent's side because no edge is
ever named.

Observed live on 2026-08-20 after the BOUNDARY-basin fix (#1774) removed the
fabricated `tight`: margin read "comfortable", nearest_edge null, and the
envelope still advised that the policy margin was near an edge.
"""
from src.mcp_handlers.middleware import envelope_step as ES


def hint(**payload):
    payload.setdefault("risk_score", 0.0)
    return ES._recovery_hint(payload, risk=payload.get("risk_score"), coherence=0.49)


def live_shape(**payload):
    """The payload a `guide` verdict actually produces.

    Critically it carries NO decision.action, so `action` falls back to the
    verdict "guide" and `continuing` is False -- meaning the attention+continuing
    branch is never reached and control lands in the attention-only tail. The
    original tests all passed decision={"action": "proceed"}, which made
    `continuing` True and exercised a branch the live path does not take. That
    is why #1775 tested green while the deployed envelope was unchanged.
    """
    payload.setdefault("risk_score", 0.05)
    return ES._recovery_hint(payload, risk=payload["risk_score"], coherence=0.49)


def test_guide_verdict_with_comfortable_margin_does_not_claim_a_near_edge():
    h = hint(verdict="guide", decision={"action": "proceed"}, margin="comfortable")
    assert h is not None
    assert "margin is near an edge" not in h, (
        "a verdict condition must not be reported as a threshold condition"
    )
    assert "advisory" in h


def test_a_genuinely_tight_margin_still_gets_the_margin_wording():
    h = hint(verdict="guide", decision={"action": "proceed"}, margin="tight")
    assert "margin is near an edge" in h


def test_settling_margin_is_not_a_near_edge_claim_either():
    h = hint(verdict="guide", decision={"action": "proceed"}, margin="settling")
    assert "margin is near an edge" not in h


def test_severe_action_still_wins_over_both():
    h = hint(verdict="pause", decision={"action": "pause"}, margin="comfortable")
    assert "degraded" in h


def test_healthy_state_still_yields_no_hint():
    assert hint(verdict="safe", decision={"action": "proceed"}, margin="comfortable") is None


# --- the branch the live path actually takes ---------------------------------

def test_live_shape_guide_verdict_with_comfortable_margin():
    h = live_shape(verdict={"value": "guide"}, margin="comfortable")
    assert "margin is near an edge" not in h
    assert "advisory" in h


def test_live_shape_guide_verdict_with_no_margin_key_at_all():
    """Mirror mode omits a comfortable margin entirely, so the key is absent."""
    h = live_shape(verdict={"value": "guide"})
    assert "margin is near an edge" not in h


def test_live_shape_preserves_the_margin_wording_when_the_margin_is_real():
    h = live_shape(verdict={"value": "guide"}, margin="tight")
    assert "margin is near an edge" in h


def test_both_branches_agree_so_neither_can_rot_alone():
    """#1775 fixed one of the two returns. Pin them together."""
    comfortable_continuing = hint(verdict="guide", decision={"action": "proceed"},
                                  margin="comfortable")
    comfortable_tail = live_shape(verdict={"value": "guide"}, margin="comfortable")
    assert "advisory" in comfortable_continuing
    assert "advisory" in comfortable_tail
