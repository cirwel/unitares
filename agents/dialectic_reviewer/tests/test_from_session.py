"""The reviewer's launch-profile mode: only the session id comes from the spawn.

inference-hosts-config-v0 §2.5: a caller who could supply the thesis or the
paused agent's state could have a reviewer with its own identity file a verdict
on a real session from invented evidence. With DIALECTIC_REVIEWER_FROM_SESSION=1
the reviewer reads both from dialectic(action='get') and refuses a record that
does not support a review.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from agents.dialectic_reviewer import reviewer
from agents.dialectic_reviewer.reviewer import (
    EXIT_NOT_REVIEWABLE,
    FROM_SESSION_ENV,
    PAUSE_EVIDENCE_SCHEMA,
    SessionNotReviewable,
    load_thesis_from_session,
    thesis_from_session,
)

SID = "s-1"
PAUSED = "11111111-1111-4111-8111-111111111111"
RECORD = {
    "success": True,
    "session_id": SID,
    "phase": "antithesis",
    "paused_agent": PAUSED,
    "topic": "Paused on risk",
    "reason": "",
    "paused_agent_state": {"schema": PAUSE_EVIDENCE_SCHEMA, "risk_score": 0.7},
    "transcript": [
        {"phase": "thesis", "agent_id": PAUSED, "root_cause": "old", "reasoning": "first"},
        {"phase": "thesis", "agent_id": "someone-else", "root_cause": "planted"},
        {"phase": "thesis", "agent_id": PAUSED, "root_cause": "load",
         "proposed_conditions": ["rerun"], "reasoning": "latest"},
    ],
}


def _record(**changes):
    record = copy.deepcopy(RECORD)
    record.update(changes)
    return record


def test_builds_the_thesis_from_the_paused_agents_latest_thesis():
    thesis, parent = thesis_from_session(SID, RECORD)
    assert parent == PAUSED
    assert (thesis.root_cause, thesis.reasoning) == ("load", "latest")
    assert thesis.proposed_conditions == ["rerun"]
    assert thesis.situation == "Topic: Paused on risk"
    assert thesis.paused_agent_state == RECORD["paused_agent_state"]


@pytest.mark.parametrize(
    "record, reason",
    [
        (None, "no record"),
        (_record(session_id="other"), "no record"),
        (_record(phase="synthesis"), "not awaiting its reviewer"),
        (_record(phase="resolved"), "not awaiting its reviewer"),
        (_record(paused_agent="unknown"), "no paused agent"),
        (_record(transcript=[{"phase": "thesis", "agent_id": "someone-else"}]), "no thesis"),
        (_record(transcript=None), "no thesis"),
        (_record(paused_agent_state={}), "no server-captured pause evidence"),
        (_record(paused_agent_state={"schema": "other"}), "no server-captured pause evidence"),
        (_record(paused_agent_state="x"), "no server-captured pause evidence"),
    ],
)
def test_refuses_a_record_that_does_not_support_a_review(record, reason):
    with pytest.raises(SessionNotReviewable, match=reason):
        thesis_from_session(SID, record)


def test_reads_the_session_before_onboarding():
    calls = []

    class FakeClient:
        def __init__(self, url):
            self.url = url

        async def connect(self):
            calls.append("connect")

        async def disconnect(self):
            calls.append("disconnect")

        async def onboard(self, **kwargs):  # pragma: no cover - must not run
            calls.append("onboard")

        async def call_tool(self, name, args, **kwargs):
            calls.append((name, args))
            return RECORD

    thesis, parent = asyncio.run(load_thesis_from_session(SID, "http://gov", FakeClient))
    assert parent == PAUSED and thesis.root_cause == "load"
    assert ("dialectic", {"action": "get", "session_id": SID}) in calls
    assert "onboard" not in calls
    assert calls[-1] == "disconnect"


@pytest.fixture
def profile_env(monkeypatch):
    monkeypatch.setenv("DIALECTIC_SESSION_ID", SID)
    monkeypatch.setenv(FROM_SESSION_ENV, "1")
    # What a launcher might try to supply; the profile mode must ignore it.
    monkeypatch.setenv("DIALECTIC_THESIS_ROOT_CAUSE", "invented")
    monkeypatch.setenv("DIALECTIC_PAUSED_AGENT_STATE", '{"risk_score": 0}')
    monkeypatch.setenv("UNITARES_PARENT_AGENT_ID", "22222222-2222-4222-8222-222222222222")


def test_main_reviews_the_session_record_not_the_environment(profile_env, monkeypatch):
    async def load(session_id, url, factory=None):
        return thesis_from_session(session_id, RECORD)

    seen = {}

    async def run(thesis, url, parent):
        seen.update(thesis=thesis, parent=parent)
        return reviewer.Verdict(agrees=False, root_cause="", proposed_conditions=[], reasoning="r")

    monkeypatch.setattr(reviewer, "load_thesis_from_session", load)
    monkeypatch.setattr(reviewer, "run", run)
    assert reviewer.main() == 0
    assert seen["thesis"].root_cause == "load"
    assert seen["thesis"].paused_agent_state == RECORD["paused_agent_state"]
    assert seen["parent"] == PAUSED


def test_main_refuses_with_its_own_exit_status(profile_env, monkeypatch, capsys):
    async def load(session_id, url, factory=None):
        raise SessionNotReviewable("the session is in phase 'resolved', not awaiting its reviewer")

    monkeypatch.setattr(reviewer, "load_thesis_from_session", load)
    monkeypatch.setattr(reviewer, "run", lambda *a: pytest.fail("reviewed anyway"))
    assert reviewer.main() == EXIT_NOT_REVIEWABLE
    assert "REFUSED" in capsys.readouterr().out


def test_main_reports_a_failed_read_as_a_crash(profile_env, monkeypatch):
    async def load(session_id, url, factory=None):
        raise ConnectionError("governance down")

    monkeypatch.setattr(reviewer, "load_thesis_from_session", load)
    monkeypatch.setattr(reviewer, "run", lambda *a: pytest.fail("reviewed anyway"))
    assert reviewer.main() == 1


def test_without_the_switch_the_spawn_payload_is_used(monkeypatch):
    monkeypatch.setenv("DIALECTIC_SESSION_ID", SID)
    monkeypatch.delenv(FROM_SESSION_ENV, raising=False)
    monkeypatch.setenv("DIALECTIC_THESIS_ROOT_CAUSE", "from env")
    monkeypatch.setattr(reviewer, "load_thesis_from_session",
                        lambda *a: pytest.fail("read the session"))
    seen = {}

    async def run(thesis, url, parent):
        seen["root_cause"] = thesis.root_cause
        return reviewer.Verdict(agrees=False, root_cause="", proposed_conditions=[], reasoning="r")

    monkeypatch.setattr(reviewer, "run", run)
    assert reviewer.main() == 0
    assert seen["root_cause"] == "from env"
