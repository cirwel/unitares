"""The synthetic reviewer's verdict must BIND.

Before this fix, `_run_synthetic_review` and `handle_llm_assisted_dialectic`
hardcoded `agrees=True` on the synthesis, so the model's RESUME/COOLDOWN/ESCALATE
recommendation was discarded and a disputed — even transparently-unsafe — thesis
still resolved RESUME (demonstrated live 2026-06-23). `_synthetic_review_approves`
maps the recommendation to a binding agree/disagree: only RESUME approves.
"""
import pytest

from src.mcp_handlers.dialectic.handlers import _synthetic_review_approves


@pytest.mark.parametrize(
    "recommendation,expected",
    [
        ("RESUME", True),
        ("resume", True),  # helper upper-cases
        (" Resume ", True),  # and strips
        ("COOLDOWN", False),
        ("ESCALATE", False),
        ("", False),  # no verdict → do not approve
        (None, False),  # missing → do not approve
        ("BLOCK", False),  # any non-RESUME token → do not approve
    ],
)
def test_only_resume_approves(recommendation, expected):
    synthesis = {"recommendation": recommendation} if recommendation is not None else {}
    assert _synthetic_review_approves(synthesis) is expected


def test_missing_synthesis_does_not_approve():
    assert _synthetic_review_approves(None) is False
    assert _synthetic_review_approves({}) is False


def test_escalate_is_the_blocking_case():
    """The regression that motivated this fix: a disputed thesis recommends
    ESCALATE, which must NOT approve (so the session does not auto-resume)."""
    assert _synthetic_review_approves({"recommendation": "ESCALATE"}) is False


# --- position-aware binding (the live-2026-06-23 rubber-stamp that survived #1015) ---

def test_dispute_with_resume_does_not_approve():
    """The two signals come from separate model calls and can disagree. A RESUME
    synthesis sitting over a `position=dispute` antithesis is the exact live
    rubber-stamp: it must FAIL CLOSED, not auto-resolve."""
    synthesis = {"recommendation": "RESUME"}
    antithesis = {"position": "dispute"}
    assert _synthetic_review_approves(synthesis, antithesis) is False


@pytest.mark.parametrize("position", ["agree", "refine", "", "Agree", " AGREE "])
def test_non_dispute_position_allows_resume(position):
    """RESUME approves when the antithesis is not a dispute (agree/refine/absent)."""
    synthesis = {"recommendation": "RESUME"}
    antithesis = {"position": position}
    assert _synthetic_review_approves(synthesis, antithesis) is True


@pytest.mark.parametrize("rec", ["COOLDOWN", "ESCALATE"])
def test_dispute_is_moot_when_recommendation_already_blocks(rec):
    """A non-RESUME recommendation blocks regardless of position."""
    assert _synthetic_review_approves({"recommendation": rec}, {"position": "dispute"}) is False
    assert _synthetic_review_approves({"recommendation": rec}, {"position": "agree"}) is False


def test_antithesis_omitted_is_backward_compatible():
    """Calling without the antithesis (legacy) still binds on recommendation only."""
    assert _synthetic_review_approves({"recommendation": "RESUME"}) is True
    assert _synthetic_review_approves({"recommendation": "ESCALATE"}) is False
