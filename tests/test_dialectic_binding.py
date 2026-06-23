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
