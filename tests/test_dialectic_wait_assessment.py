"""Tests for dialectic wait assessment — the WHEN half of `whose_move`.

`whose_move` was added because a live session waiting on the caller's own
synthesis was misread as stalled by two experienced operators (2026-07-28). It
answered WHO. It never answered WHEN, so the open-slot text read identically at
72 seconds and at 72 minutes, and on 2026-09-13 that produced two wrong calls
inside a single session: a reviewer polled at 72s and 2m15s looked absent both
times and was mid-model-call, arriving at ~2m20s.

These tests pin the property that fixes: a wait inside the reviewer's own
declared budget must never be presented as grounds to escalate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import src.mcp_handlers.context as ctx
import src.mcp_handlers.dialectic.handlers as handlers
from src.mcp_handlers.dialectic import wait_assessment as wa


def _view(seconds_ago: float, *, phase: str = "antithesis", reviewer=None):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    data = {
        "session_id": "s",
        "phase": phase,
        "paused_agent": "A",
        "transcript": [{"phase": "thesis", "agent_id": "A", "timestamp": ts}],
    }
    if reviewer:
        data["reviewer"] = reviewer
    return data


# --- the classifier ----------------------------------------------------------


def test_inside_budget_is_too_early():
    out = wa.assess_wait(elapsed_s=72.0, awaiting="verdict")
    assert out["assessment"] == wa.TOO_EARLY


def test_past_budget_is_overdue():
    out = wa.assess_wait(elapsed_s=10_000.0, awaiting="verdict")
    assert out["assessment"] == wa.OVERDUE


def test_budget_covers_the_model_call_ceiling():
    """The budget must exceed the reviewer's own allowed model-call time.

    If it did not, a reviewer using its full allowance would be declared overdue
    while behaving exactly as configured.
    """
    assert wa.expected_budget_s(awaiting="verdict") > wa._verdict_timeout_s()
    assert wa.expected_budget_s(awaiting="reconsideration") > wa._verdict_timeout_s()


def test_budget_tracks_the_operator_override():
    with patch.dict("os.environ", {"UNITARES_DIALECTIC_CODEX_TIMEOUT_S": "900"}):
        assert wa.expected_budget_s(awaiting="verdict") > 900


def test_invalid_override_falls_back_rather_than_crashing():
    with patch.dict("os.environ", {"UNITARES_DIALECTIC_CODEX_TIMEOUT_S": "not-a-number"}):
        assert wa.expected_budget_s(awaiting="verdict") > 0


def test_unknown_elapsed_is_unclassified_not_healthy():
    """A missing clock and a short wait are different findings."""
    out = wa.assess_wait(elapsed_s=None, awaiting="verdict")
    assert out["assessment"] is None
    assert out["elapsed_s"] is None
    assert "not" in out["note"].lower()


def test_unorchestrated_reviewer_has_no_deadline_to_miss():
    """A human reviewer never declared a budget, so nothing can be late."""
    out = wa.assess_wait(elapsed_s=10_000.0, awaiting="verdict", orchestrated=False)
    assert out["assessment"] is None
    assert out["expected_by_s"] is None


# --- the escalation gate -----------------------------------------------------


@pytest.mark.parametrize(
    "assessment,expected",
    [(wa.OVERDUE, True), (wa.TOO_EARLY, False), (None, False), ("anything", False)],
)
def test_only_overdue_may_suggest_facilitation(assessment, expected):
    assert wa.suggests_facilitation(assessment) is expected


# --- the handler surface -----------------------------------------------------


def test_open_slot_inside_budget_says_do_not_escalate():
    """The exact 2026-09-13 misread: a 72s wait must not invite facilitation."""
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(72))
    assert out["wait_assessment"]["assessment"] == wa.TOO_EARLY
    assert "do not escalate yet" in out["whose_move"]
    assert "ask for facilitation" not in out["whose_move"]


def test_open_slot_past_budget_restores_the_facilitation_hint():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(10_000))
    assert out["wait_assessment"]["assessment"] == wa.OVERDUE
    assert "facilitation" in out["whose_move"]


def test_every_session_view_carries_the_block():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(5))
    assert "wait_assessment" in out
    assert set(out["wait_assessment"]) >= {"elapsed_s", "expected_by_s", "assessment", "note"}


def test_empty_transcript_does_not_fabricate_a_clock():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(
            {"session_id": "s", "phase": "antithesis", "paused_agent": "A", "transcript": []}
        )
    assert out["wait_assessment"]["elapsed_s"] is None
    assert out["wait_assessment"]["assessment"] is None


def test_unparseable_timestamp_is_skipped_not_guessed():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(
            {
                "session_id": "s",
                "phase": "antithesis",
                "paused_agent": "A",
                "transcript": [{"phase": "thesis", "agent_id": "A", "timestamp": "not-a-date"}],
            }
        )
    assert out["wait_assessment"]["elapsed_s"] is None


def test_assessment_never_changes_who_may_submit():
    """This is a reporting change. It must not touch authorization."""
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        early = handlers._build_dialectic_actionability(_view(5))
        late = handlers._build_dialectic_actionability(_view(10_000))
    for key in ("allowed_agent_ids", "required_role", "current_agent_can_submit"):
        assert early[key] == late[key], f"{key} changed with elapsed time"
