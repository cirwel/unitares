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
        "author_agent_uuid": UUID_A, "author_model": "claude-test-1",
        "first_seen": 1_000_000.0, "announced": False,
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


def test_opened_pr_posts_one_finding_naming_the_author(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    assert _emit(state) == {"opened": 1, "resolved": 0, "failed": 0, "deferred": 0}
    (call,) = calls
    assert call["event_type"] == "vigil_finding" and call["severity"] == "warning"
    assert KEY in call["message"] and UUID_A in call["message"]
    assert call["extra"]["author_agent_uuid"] == UUID_A
    assert call["extra"]["finding_type"] == "stalled_draft_pr"
    assert call["extra"]["stall_fingerprint"] == call["fingerprint"]


def test_unsigned_pr_is_reported_with_no_author(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry(author_agent_uuid="", author_model="")},
             "stalled_pr_opened": [KEY], "stalled_pr_resolved": []}
    _emit(state)
    assert "no agent signature" in calls[0]["message"]
    assert calls[0]["extra"]["author_agent_uuid"] == ""


def test_still_open_pr_is_not_posted_again(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {KEY: _entry(announced=True)}, "stalled_pr_opened": [],
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
    assert _emit(state) == {"opened": 0, "resolved": 1, "failed": 0, "deferred": 0}
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


def test_failed_open_keeps_its_episode_and_is_resent(posts):
    """Review finding: dropping the entry would mint a new episode on retry
    (defeating server dedup) and lose the closure if the PR merged first."""
    calls, outcome = posts
    outcome["value"] = FAILED
    state = {"stalled_pr_open": {KEY: _entry()}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": []}
    assert _emit(state) == {"opened": 0, "resolved": 0, "failed": 1, "deferred": 0}
    assert state["stalled_pr_open"][KEY]["announced"] is False
    first_token = calls[0]["change_token"]

    # Next cycle the check no longer lists it as newly opened; it is still sent.
    outcome["value"] = DELIVERED
    calls.clear()
    state["stalled_pr_opened"] = []
    assert _emit(state)["opened"] == 1
    assert calls[0]["change_token"] == first_token
    assert state["stalled_pr_open"][KEY]["announced"] is True


def test_closure_of_a_never_announced_stall_says_so(posts):
    calls, _ = posts
    state = {"stalled_pr_open": {}, "stalled_pr_opened": [],
             "stalled_pr_resolved": [{**_entry(), "outcome": "merged", "open_hours": 0.5}]}
    _emit(state)
    assert calls[0]["extra"]["stall_announced"] is False


def test_failed_closure_is_kept_pending_and_resent(posts):
    """Review finding: a failed closure must not go back on the open list,
    where a PR that went red again would swallow it."""
    calls, outcome = posts
    outcome["value"] = FAILED
    row = {**_entry(), "outcome": "retriggered", "open_hours": 3.0}
    state = {"stalled_pr_open": {}, "stalled_pr_opened": [], "stalled_pr_resolved": [row]}
    assert _emit(state)["failed"] == 1
    assert state["stalled_pr_open"] == {}
    assert state["stalled_pr_pending_closures"] == [row]

    # Next cycle: the PR has stalled again as a new episode, governance is back.
    outcome["value"] = DELIVERED
    calls.clear()
    again = _entry(first_seen=1_050_000.0)
    state = {"stalled_pr_open": {KEY: again}, "stalled_pr_opened": [KEY],
             "stalled_pr_resolved": [], "stalled_pr_pending_closures": [row]}
    counts = _emit(state)
    assert counts["opened"] == 1 and counts["resolved"] == 1
    assert state["stalled_pr_pending_closures"] == []
    kinds = [(c["event_type"], c["change_token"]) for c in calls]
    assert ("vigil_finding", "open:1050000.0") in kinds
    assert ("vigil_resolution_finding", "resolved:1000000.0") in kinds
    assert state["stalled_pr_open"][KEY]["first_seen"] == 1_050_000.0
    assert state["stalled_pr_open"][KEY]["announced"] is True


def test_pending_closures_are_bounded(posts, monkeypatch):
    from agents.vigil import agent as vigil_mod

    _, outcome = posts
    outcome["value"] = FAILED
    monkeypatch.setattr(vigil_mod, "STALLED_PR_MAX_PENDING_CLOSURES", 2)
    rows = [{**_entry(number=n), "outcome": "merged", "open_hours": 1.0} for n in (1, 2, 3)]
    state = {"stalled_pr_open": {}, "stalled_pr_opened": [], "stalled_pr_resolved": rows}
    _emit(state)
    assert [r["number"] for r in state["stalled_pr_pending_closures"]] == [2, 3]


@pytest.mark.parametrize("state", [{}, {"stalled_pr_open": None}, {"stalled_pr_open": {},
                         "stalled_pr_opened": ["missing"], "stalled_pr_resolved": ["junk"]}])
def test_malformed_state_does_not_raise_or_post(posts, state):
    calls, _ = posts
    assert _emit(state) == {"opened": 0, "resolved": 0, "failed": 0, "deferred": 0}
    assert calls == []


def _many(n: int) -> dict:
    keys = [f"acmeco/unitares#{i}" for i in range(1, n + 1)]
    return {
        "stalled_pr_open": {k: _entry(number=i) for i, k in enumerate(keys, 1)},
        "stalled_pr_opened": list(keys),
        "stalled_pr_resolved": [],
    }


def test_first_failed_post_stops_the_pass_and_undoes_the_rest(posts):
    """Governance down: one attempt, not one slow attempt per stalled PR."""
    calls, outcome = posts
    outcome["value"] = FAILED
    state = _many(15)
    counts = _emit(state)
    assert len(calls) == 1
    assert counts == {"opened": 0, "resolved": 0, "failed": 1, "deferred": 14}
    assert len(state["stalled_pr_open"]) == 15  # all kept, none announced
    assert not any(e["announced"] for e in state["stalled_pr_open"].values())


def test_emit_budget_defers_what_does_not_fit(posts, monkeypatch):
    from agents.vigil import agent as vigil_mod

    calls, _ = posts
    clock = {"t": 0.0}
    monkeypatch.setattr(vigil_mod, "STALLED_PR_EMIT_BUDGET", 10.0)
    monkeypatch.setattr(vigil_mod.time, "monotonic", lambda: clock["t"])
    real = vigil_mod.post_finding_result

    def slow_post(**kwargs):
        clock["t"] += 6.0
        return real(**kwargs)

    monkeypatch.setattr(vigil_mod, "post_finding_result", slow_post)
    state = _many(5)
    state["stalled_pr_resolved"] = [
        {**_entry(number=99), "outcome": "merged", "open_hours": 2.0}
    ]
    counts = _emit(state)
    assert len(calls) == 2
    assert counts["opened"] == 2 and counts["deferred"] == 4 and counts["failed"] == 0
    announced = sorted(k for k, e in state["stalled_pr_open"].items() if e["announced"])
    assert announced == ["acmeco/unitares#1", "acmeco/unitares#2"]
    assert len(state["stalled_pr_open"]) == 5
    assert [r["number"] for r in state["stalled_pr_pending_closures"]] == [99]
