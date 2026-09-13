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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.mcp_handlers.context as ctx
import src.mcp_handlers.dialectic.handlers as handlers
from src.mcp_handlers.dialectic import wait_assessment as wa


@pytest.fixture(autouse=True)
def runner_metadata(monkeypatch):
    # Replace the handler's proxy reference, not a delegated proxy attribute.
    # Restoring an attribute resolved through __getattr__ would leave a shadow
    # attribute on the shared proxy and bypass later get_mcp_server patches.
    monkeypatch.setattr(handlers, "mcp_server", SimpleNamespace(agent_metadata={
        "B": {"model_type": "dialectic_reviewer:configured-model"},
    }))


def _view(seconds_ago: float, *, phase: str = "antithesis", reviewer="B"):
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
    out = wa.assess_wait(elapsed_s=72.0, awaiting="verdict", orchestrated=True)
    assert out["assessment"] == wa.TOO_EARLY


def test_past_budget_is_overdue():
    out = wa.assess_wait(elapsed_s=10_000.0, awaiting="verdict", orchestrated=True)
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
    out = wa.assess_wait(elapsed_s=None, awaiting="verdict", orchestrated=True)
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


def test_identified_runner_inside_budget_is_too_early():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(72))
    assert out["wait_assessment"]["assessment"] == wa.TOO_EARLY
    assert "ask for facilitation" not in out["whose_move"]


def test_identified_runner_past_budget_is_overdue():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(10_000))
    assert out["wait_assessment"]["assessment"] == wa.OVERDUE
    assert "worth investigating" in out["wait_assessment"]["note"]


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


@pytest.mark.parametrize("phase", ["resolved", "failed", "escalated", "timeout", "abandoned", "quorum_voting", "thesis", "synthesis"])
@pytest.mark.parametrize("elapsed", [72, 10_000])
def test_no_reviewer_obligation_has_no_deadline(phase, elapsed):
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(elapsed, phase=phase))
    assert out["wait_assessment"]["assessment"] is None
    assert out["wait_assessment"]["expected_by_s"] is None


@pytest.mark.parametrize("elapsed", [72, 10_000])
def test_open_slot_has_no_identified_reviewer_budget(elapsed):
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(elapsed, reviewer=None))
    assert out["wait_assessment"]["assessment"] is None
    assert out["wait_assessment"]["expected_by_s"] is None
    assert "inside" not in out["whose_move"]
    assert "do not escalate" not in out["whose_move"]


def test_assigned_unmanaged_reviewer_does_not_acquire_runner_budget():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_view(10_000, reviewer="human"))
    assert out["wait_assessment"]["assessment"] is None
    assert out["wait_assessment"]["expected_by_s"] is None


def test_reviewer_calling_on_own_turn_is_not_a_wait():
    with patch.object(ctx, "get_context_agent_id", return_value="B"):
        out = handlers._build_dialectic_actionability(_view(10_000))
    assert out["whose_move"].startswith("YOURS")
    assert out["wait_assessment"]["assessment"] is None


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2999-01-01T00:00:00Z"])
def test_unreadable_newer_message_cannot_reuse_old_clock(stamp):
    data = _view(10_000)
    data["transcript"].append({"phase": "system", "timestamp": stamp})
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["elapsed_s"] is None
    assert out["wait_assessment"]["assessment"] is None
    assert "inside" not in out["whose_move"]


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), -float("inf"), "100", True])
def test_invalid_elapsed_is_unknown(elapsed):
    out = wa.assess_wait(elapsed_s=elapsed, awaiting="verdict", orchestrated=True)
    assert out["elapsed_s"] is None
    assert out["assessment"] is None


@pytest.mark.parametrize("value", ["inf", "nan", "0", "-1"])
def test_nonfinite_or_nonpositive_timeout_uses_default(value):
    with patch.dict("os.environ", {"UNITARES_DIALECTIC_CODEX_TIMEOUT_S": value}):
        assert wa._verdict_timeout_s() == wa.DEFAULT_VERDICT_TIMEOUT_S


def _synthesis_view(*, paused_response=False, verdict=None):
    data = _view(10_000, phase="synthesis")
    stamp = data["transcript"][0]["timestamp"]
    data["transcript"].append({"phase": "antithesis", "agent_id": "B", "timestamp": stamp})
    if verdict is not None:
        data["transcript"].append({"phase": "synthesis", "agent_id": "B", "agrees": verdict, "timestamp": stamp})
    if paused_response:
        data["transcript"].append({"phase": "synthesis", "agent_id": "A", "agrees": True, "timestamp": stamp})
    return data


def test_first_reviewer_synthesis_uses_verdict_budget():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_synthesis_view())
    assert out["reviewer_verdict_pending"] is True
    assert out["wait_assessment"]["expected_by_s"] == wa.expected_budget_s(awaiting="verdict")
    assert out["wait_assessment"]["assessment"] == wa.OVERDUE


def test_caller_response_to_rejection_is_not_reviewer_wait():
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(_synthesis_view(verdict=False))
    assert out["required_role"] == "paused_agent"
    assert out["wait_assessment"]["assessment"] is None
    assert out["wait_assessment"]["expected_by_s"] is None


def test_reconsideration_wait_starts_after_paused_response():
    data = _synthesis_view(verdict=False, paused_response=True)
    data["transcript"][-1]["timestamp"] = datetime.now(timezone.utc).isoformat()
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["expected_by_s"] == wa.expected_budget_s(awaiting="reconsideration")
    assert out["wait_assessment"]["assessment"] == wa.TOO_EARLY


@pytest.mark.parametrize("status", ["resolved", "failed", "timeout", "abandoned"])
@pytest.mark.parametrize("phase", ["antithesis", "synthesis"])
def test_terminal_status_does_not_inherit_stale_phase_wait(status, phase):
    data = _synthesis_view() if phase == "synthesis" else _view(10_000)
    data["status"] = status
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["assessment"] is None


def test_object_messages_and_runner_metadata_are_supported(monkeypatch):
    monkeypatch.setattr(handlers.mcp_server, "agent_metadata", {
        "B": SimpleNamespace(model_type="dialectic_reviewer:configured-model"),
    })
    data = _synthesis_view()
    data["messages"] = [SimpleNamespace(**message) for message in data.pop("transcript")]
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["assessment"] == wa.OVERDUE


def test_reassigned_human_does_not_inherit_previous_runner_provenance():
    data = _synthesis_view()
    data["transcript"][-1]["observed_metrics"] = {"reviewer_backend": {"reviewer_kind": "orchestrated"}}
    data["reviewer"] = "human"
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["assessment"] is None


def test_budget_is_not_assumed_by_default():
    out = wa.assess_wait(elapsed_s=10_000, awaiting="verdict")
    assert out["assessment"] is None
    assert out["expected_by_s"] is None


def test_explicit_runner_provenance_works_without_cached_identity(monkeypatch):
    monkeypatch.setattr(handlers.mcp_server, "agent_metadata", {})
    data = _synthesis_view()
    data["transcript"][-1]["observed_metrics"] = {"reviewer_backend": {"reviewer_kind": "orchestrated"}}
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["assessment"] == wa.OVERDUE


def test_external_consult_does_not_inherit_runner_timeout():
    data = _synthesis_view()
    data["transcript"][-1]["observed_metrics"] = {"reviewer_backend": {"reviewer_kind": "external_consult"}}
    with patch.object(ctx, "get_context_agent_id", return_value="A"):
        out = handlers._build_dialectic_actionability(data)
    assert out["wait_assessment"]["assessment"] is None
