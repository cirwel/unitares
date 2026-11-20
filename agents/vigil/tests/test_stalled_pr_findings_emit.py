"""Vigil posts one finding per stalled draft PR opened, and one per PR closed.

``emit_stalled_pr_findings`` reads the edges the check left in the cycle's
health state. The cases that matter are the failure ones: a post that did not
reach governance must leave the state such that the next cycle tries again.
"""

from __future__ import annotations

import pytest

from agents.common.findings import DEDUPED, DELIVERED, FAILED

UUID_A = "11111111-2222-3333-4444-555555555555"
KEY = "acmeco/unitares#7"


def _entry(**kw) -> dict:
    base = {
        "repo": "acmeco/unitares", "number": 7, "url": "https://example.test/7",
        "failing": ["smoke", "lint"], "red_hours": 30.0,
        "owner_agent_uuid": UUID_A, "owner_model": "claude-test-1",
        "first_seen": 1_000_000.0,
    }
    base.update(kw)
    return base


@pytest.fixture
def posts(monkeypatch):
    from agents.vigil import agent as vigil_mod

    calls = []
    outcome = {"value": DELIVERED}

    def fake_post(**kwargs):
        calls.append(kwargs)
        return outcome["value"]

    monkeypatch.setattr(vigil_mod, "post_finding_result", fake_post)
    monkeypatch.setattr(vigil_mod, "notify", lambda *a, **k: None)
    calls_and_outcome = (calls, outcome)
    return calls_and_outcome


def _emit(state):
    from agents.vigil.agent import emit_stalled_pr_findings

    return emit_stalled_pr_findings(state)


def test_opened_pr_posts_one_finding_naming_the_owner(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    assert _emit(state) == {"opened": 1, "resolved": 0, "failed": 0}
    (call,) = calls
    assert call["event_type"] == "vigil_finding" and call["severity"] == "warning"
    assert KEY in call["message"] and UUID_A in call["message"]
    assert call["extra"]["owner_agent_uuid"] == UUID_A
    assert call["extra"]["finding_type"] == "stalled_draft_pr"
    assert call["extra"]["stall_fingerprint"] == call["fingerprint"]


def test_unsigned_pr_is_reported_with_no_owner(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry(owner_agent_uuid="", owner_model="")},
             "stalled_pr_opened": [KEY], "stalled_pr_resolved": []}
    _emit(state)
    assert "no agent signature" in calls[0]["message"]
    assert calls[0]["extra"]["owner_agent_uuid"] == ""


def test_still_open_pr_is_not_posted_again(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [],
             "stalled_pr_resolved": []}
    assert _emit(state)["opened"] == 0
    assert calls == []


def test_resolved_pr_posts_a_closure_paired_to_its_stall(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    _emit(state)
    stall_fp = calls[0]["fingerprint"]
    calls.clear()

    state = {"stalled_pr_open": {}, "stalled_pr_opened": [],
             "stalled_pr_resolved": [{**_entry(), "outcome": "merged", "open_hours": 41.5}]}
    assert _emit(state) == {"opened": 0, "resolved": 1, "failed": 0}
    (call,) = calls
    assert call["event_type"] == "vigil_resolution_finding" and call["severity"] == "info"
    assert call["extra"]["stall_fingerprint"] == stall_fp
    assert call["fingerprint"] != stall_fp  # server dedup is per fingerprint
    assert call["extra"]["outcome"] == "merged" and call["extra"]["open_hours"] == 41.5


def test_deduped_counts_as_reached(posts):
    calls, outcome = posts
    outcome["value"] = DEDUPED
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    assert _emit(state)["opened"] == 1
    assert KEY in state["stalled_pr_open"]


def test_failed_open_is_dropped_so_it_reopens_next_cycle(posts):
    _, outcome = posts
    outcome["value"] = FAILED
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    assert _emit(state) == {"opened": 0, "resolved": 0, "failed": 1}
    assert KEY not in state["stalled_pr_open"]


def test_failed_closure_is_put_back_so_it_is_retried(posts):
    _, outcome = posts
    outcome["value"] = FAILED
    state = {"stalled_pr_open": {}, "stalled_pr_opened": [],
             "stalled_pr_resolved": [{**_entry(), "outcome": "merged", "open_hours": 3.0}]}
    assert _emit(state)["failed"] == 1
    assert state["stalled_pr_open"][KEY]["first_seen"] == 1_000_000.0
    assert "outcome" not in state["stalled_pr_open"][KEY]


@pytest.mark.parametrize("state", [{}, {"stalled_pr_open": None}, {"stalled_pr_open": {},
                         "stalled_pr_opened": ["missing"], "stalled_pr_resolved": ["junk"]}])
def test_malformed_state_does_not_raise_or_post(posts, state):
    calls, _ = posts
    assert _emit(state) == {"opened": 0, "resolved": 0, "failed": 0}
    assert calls == []
