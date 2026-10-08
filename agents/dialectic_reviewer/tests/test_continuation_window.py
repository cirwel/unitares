"""The reviewer's response window restarts after every synthesis it files.

Live instance: session 8973efacebb17e11 (2026-10-08). The reviewer filed its
first rejection at 04:49 and its second at 05:06. The paused agent conceded in
full at 05:56 -- 67 minutes after the first rejection. The reviewer had one
one-hour budget for the whole continuation, so it exited at 05:49 and the
concession waited unread until the 4h liveness reap marked the session failed.
The protocol's one-hour window is per synthesis response, so the reviewer's
wait must be too.
"""

import asyncio

import pytest

import agents.dialectic_reviewer.reviewer as r
from agents.dialectic_reviewer.reviewer import Thesis, Verdict

REVIEWER = "reviewer-uuid"
PAUSED = "paused-uuid"
WAIT_S = 100.0
POLL_S = 10.0


def _msg(agent, agrees):
    return {"phase": "synthesis", "agent_id": agent, "agrees": agrees,
            "root_cause": "x", "proposed_conditions": [], "reasoning": "r"}


class _World:
    """A virtual clock plus a session whose paused agent answers on a schedule."""

    def __init__(self, answers_at):
        self.now = 0.0
        self.answers_at = list(answers_at)  # clock times of paused-agent answers
        self.transcript = [_msg(REVIEWER, False)]  # the first rejection, at t=0
        self.filings = 0

    def tick(self):
        while self.answers_at and self.now >= self.answers_at[0]:
            self.answers_at.pop(0)
            self.transcript.append(_msg(PAUSED, True))

    async def call_tool(self, name, args, **kw):
        self.tick()
        if args["action"] == "get":
            return {"paused_agent_id": PAUSED, "reviewer_agent_id": REVIEWER,
                    "phase": "synthesis", "synthesis_round": len(self.transcript),
                    "max_synthesis_rounds": 50, "transcript": list(self.transcript)}
        assert args["action"] == "synthesis"
        self.filings += 1
        self.transcript.append(_msg(REVIEWER, args["agrees"]))
        return {"success": True}


def _run(monkeypatch, world, total_wait_factor=None):
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", str(WAIT_S))
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", str(POLL_S))
    if total_wait_factor is not None:
        monkeypatch.setattr(r, "CONTINUATION_TOTAL_WAIT_FACTOR", total_wait_factor)
    monkeypatch.setattr(r.time, "monotonic", lambda: world.now)

    async def fake_sleep(seconds):
        world.now += seconds

    monkeypatch.setattr(r.asyncio, "sleep", fake_sleep)

    async def fake_obtain(prompt, pinned=None):
        return r.ReviewerText(
            '{"agrees": false, "root_cause": "x", "proposed_conditions": ["c"], "reasoning": "still no"}',
            {"backend": "codex", "host_id": "codex:host-adapter", "models_used": [],
             "warnings": [], "vouched": True, "vouched_by": "listed_host"},
            "codex",
        )

    monkeypatch.setattr(r, "obtain_reviewer_text", fake_obtain)
    thesis = Thesis(session_id="s", root_cause="", proposed_conditions=[], reasoning="")
    initial = Verdict(agrees=False, root_cause="x", proposed_conditions=["c"], reasoning="no")
    return asyncio.run(r.continue_after_disagreement(
        world, thesis, initial, paused_agent_id=PAUSED, reviewer_agent_id=REVIEWER))


def test_an_answer_after_the_first_budget_is_still_reconsidered(monkeypatch):
    # Answers at 60s and 150s: the second is past the original 100s budget but
    # within 100s of the reviewer's reply to the first.
    world = _World(answers_at=[60.0, 150.0])
    _run(monkeypatch, world)
    assert world.filings == 2


def test_the_reviewer_still_exits_one_window_after_its_last_filing(monkeypatch):
    world = _World(answers_at=[60.0, 500.0])
    _run(monkeypatch, world)
    assert world.filings == 1  # the 500s answer is nobody's to read


def test_restarting_the_wait_is_bounded_by_the_total_factor(monkeypatch):
    # The paused agent answers every 90s forever; the loop must still end.
    world = _World(answers_at=[90.0 * i for i in range(1, 40)])
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.now <= WAIT_S * 3 + POLL_S + 1
