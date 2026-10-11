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

    def __init__(self, answers_at, *, lose_synthesis_replies=False, reconciliation_reads_fail=False):
        self.now = 0.0
        self.answers_at = list(answers_at)  # clock times of paused-agent answers
        self.lose_synthesis_replies = lose_synthesis_replies
        self.reconciliation_reads_fail = reconciliation_reads_fail
        self.transcript = [_msg(REVIEWER, False)]  # the first rejection, at t=0
        self.filings = 0
        self.model_calls = 0
        self.model_latency = 0.0
        self.get_latency = 0.0
        self.response_read_latency = 0.0
        self.sleep_overshoot = 0.0
        self.filing_times = []

    def tick(self):
        while self.answers_at and self.now >= self.answers_at[0]:
            self.answers_at.pop(0)
            self.transcript.append(_msg(PAUSED, True))

    async def call_tool(self, name, args, **kw):
        self.tick()
        if args["action"] == "get":
            if self.filings and self.reconciliation_reads_fail:
                raise ConnectionError("server unavailable during reconciliation")
            self.now += self.get_latency
            if self.transcript[-1]["agent_id"] == PAUSED:
                self.now += self.response_read_latency
            return {"paused_agent_id": PAUSED, "reviewer_agent_id": REVIEWER,
                    "phase": "synthesis", "synthesis_round": len(self.transcript),
                    "max_synthesis_rounds": 50, "transcript": list(self.transcript)}
        assert args["action"] == "synthesis"
        self.filings += 1
        self.filing_times.append(self.now)
        self.transcript.append(_msg(REVIEWER, args["agrees"]))
        if self.lose_synthesis_replies:
            raise ConnectionError("synthesis landed but its reply was lost")
        return {"success": True}


def _run(monkeypatch, world, total_wait_factor=None):
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", str(WAIT_S))
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", str(POLL_S))
    if total_wait_factor is not None:
        monkeypatch.setattr(r, "CONTINUATION_TOTAL_WAIT_FACTOR", total_wait_factor)
    monkeypatch.setattr(r.time, "monotonic", lambda: world.now)

    async def fake_sleep(seconds):
        world.now += seconds + world.sleep_overshoot

    monkeypatch.setattr(r.asyncio, "sleep", fake_sleep)

    async def fake_obtain(prompt, pinned=None):
        world.model_calls += 1
        return r.ReviewerText(
            '{"agrees": false, "root_cause": "x", "proposed_conditions": ["c"], "reasoning": "still no"}',
            {"backend": "codex", "host_id": "codex:host-adapter", "models_used": [],
             "warnings": [], "vouched": True, "vouched_by": "listed_host"},
            "codex",
        )

    async def fake_wait_for(awaitable, timeout):
        if world.model_latency > timeout:
            awaitable.close()
            world.now += timeout
            raise asyncio.TimeoutError
        world.now += world.model_latency
        return await awaitable

    monkeypatch.setattr(r.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(r, "obtain_reviewer_text", fake_obtain)
    thesis = Thesis(session_id="s", root_cause="", proposed_conditions=[], reasoning="")
    initial = Verdict(agrees=False, root_cause="x", proposed_conditions=["c"], reasoning="no")
    return asyncio.run(r.continue_after_disagreement(
        world, thesis, initial, paused_agent_id=PAUSED, reviewer_agent_id=REVIEWER))


@pytest.mark.parametrize("lose_synthesis_replies", [False, True])
@pytest.mark.parametrize("first_answer", [60.0, 90.0, 99.0])
def test_an_answer_after_the_first_budget_is_still_reconsidered(
    monkeypatch, lose_synthesis_replies, first_answer
):
    # The 150s answer is past the original budget but inside the next window.
    # A first answer at 90s makes a lost reply reach the old deadline exactly.
    world = _World(answers_at=[first_answer, 150.0], lose_synthesis_replies=lose_synthesis_replies)
    _run(monkeypatch, world)
    assert world.filings == 2
    assert world.model_calls == 2  # reconciliation neither re-files nor re-judges


@pytest.mark.parametrize("lose_synthesis_replies", [False, True])
def test_the_reviewer_still_exits_one_window_after_its_last_filing(monkeypatch, lose_synthesis_replies):
    world = _World(answers_at=[60.0, 500.0], lose_synthesis_replies=lose_synthesis_replies)
    _run(monkeypatch, world)
    assert world.filings == 1  # the 500s answer is nobody's to read
    assert 60.0 + WAIT_S <= world.now <= 60.0 + WAIT_S + POLL_S


@pytest.mark.parametrize("lose_synthesis_replies", [False, True])
def test_restarting_the_wait_is_bounded_by_the_total_factor(monkeypatch, lose_synthesis_replies):
    # The paused agent answers every 90s forever; the loop must still end.
    world = _World(answers_at=[90.0 * i for i in range(1, 40)],
                   lose_synthesis_replies=lose_synthesis_replies)
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.now <= WAIT_S * 3 + POLL_S + 1


@pytest.mark.parametrize("lose_synthesis_replies", [False, True])
def test_read_outage_stops_at_the_applicable_deadline(monkeypatch, lose_synthesis_replies):
    world = _World(answers_at=[90.0], lose_synthesis_replies=lose_synthesis_replies,
                   reconciliation_reads_fail=True)
    _run(monkeypatch, world, total_wait_factor=3)
    expected_end = WAIT_S * 3 if lose_synthesis_replies else 90.0 + WAIT_S
    assert world.now == expected_end
    assert world.filings == world.model_calls == 1


def test_a_timely_answer_can_be_reviewed_after_its_response_window(monkeypatch):
    world = _World(answers_at=[90.0])
    world.model_latency = 20.0
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.filings == world.model_calls == 1
    assert world.filing_times == [110.0]


def test_model_work_cannot_overrun_the_hard_deadline(monkeypatch, caplog):
    world = _World(answers_at=[90.0])
    world.model_latency = 250.0
    verdict = _run(monkeypatch, world, total_wait_factor=3)
    assert world.now == WAIT_S * 3
    assert world.filings == 0
    assert verdict.agrees is False
    assert "continuation model exhausted" in caplog.text


def test_a_reply_during_the_last_sleep_survives_scheduler_overshoot(monkeypatch):
    world = _World(answers_at=[99.5])
    world.sleep_overshoot = 1.0
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.filings == world.model_calls == 1
    assert world.filing_times == [101.0]


def test_a_read_crossing_the_response_deadline_keeps_the_retrieved_reply(monkeypatch):
    world = _World(answers_at=[90.0])
    world.response_read_latency = 20.0
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.filings == world.model_calls == 1
    assert world.filing_times == [110.0]


def test_an_empty_read_crossing_the_deadline_gets_a_final_poll(monkeypatch):
    world = _World(answers_at=[99.0])
    world.get_latency = 13.0
    _run(monkeypatch, world, total_wait_factor=3)
    assert world.filings == world.model_calls == 1
    assert world.filing_times == [118.0]
