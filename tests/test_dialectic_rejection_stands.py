"""Facilitation exit: close a session once the reviewer's rejection stands.

Operator decision 2026-10-11 ("why is dialectic waiting on me; fix"). A session
awaiting facilitation over a standing reviewer rejection closes as
``rejection_stands`` once the reviewer has had its last word, instead of
waiting four hours for an operator who, across 27 real sessions in 60 days,
never changed an outcome.

What these tests pin:

* (a) the paused agent answers after the reviewer's window closed: terminal
  ``rejection_stands``, not awaiting facilitation (closed by the sweeper);
* (b) the window is still open: nothing changes;
* (c) the exit never resolves in the paused agent's favour;
* (d) the closed row classifies as a standing disagreement, never RESOLVED.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_outcomes import (  # noqa: E402
    FAILED,
    RESOLVED,
    UNRESOLVED_AWAITING_FACILITATION,
    classify_outcome,
    resolution_rate,
)
from src.dialectic_protocol import (  # noqa: E402
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
)
from src.mcp_handlers.dialectic.rejection_stands import (  # noqa: E402
    ACCEPTED,
    BASIS_REVIEWER_ANSWERED,
    BASIS_WINDOW_ENDED,
    CONTESTED,
    LAST_WORD_GRACE_S,
    NO_REPLY,
    REJECTION_STANDS,
    assess_rejection_stands,
)

PAUSED = "agent-paused"
REVIEWER = "agent-reviewer"
WAIT_S = 3600.0
TOTAL_S = 3 * WAIT_S
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ORCHESTRATED_STAMP = {"reviewer_backend": {"reviewer_kind": "orchestrated"}}

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
AUTO_RESOLVE = "src.mcp_handlers.dialectic.auto_resolve"


def _at(minutes_before_now: float) -> str:
    return (NOW - timedelta(minutes=minutes_before_now)).isoformat()


def _msg(phase, agent_id, minutes_before_now, agrees=None, **extra):
    return DialecticMessage(
        phase=phase,
        agent_id=agent_id,
        timestamp=_at(minutes_before_now),
        root_cause="cause" if phase != "antithesis" else None,
        reasoning="reasoning",
        proposed_conditions=["c1"] if phase != "antithesis" else None,
        agrees=agrees,
        **extra,
    )


def _assess(transcript, *, awaiting=True, orchestrated=True, now=NOW,
            reviewer=REVIEWER):
    return assess_rejection_stands(
        transcript,
        paused_agent_id=PAUSED,
        reviewer_agent_id=reviewer,
        awaiting_facilitation=awaiting,
        now=now,
        is_orchestrated=lambda _rid: orchestrated,
        wait_s=WAIT_S,
        total_s=TOTAL_S,
    )


def _base(rejected_minutes_ago):
    """thesis, antithesis, reviewer rejection `rejected_minutes_ago` minutes back."""
    return [
        _msg("thesis", PAUSED, rejected_minutes_ago + 10),
        _msg("antithesis", REVIEWER, rejected_minutes_ago + 1),
        _msg("synthesis", REVIEWER, rejected_minutes_ago, agrees=False),
    ]


# ---------------------------------------------------------------------------
# The pure assessment
# ---------------------------------------------------------------------------


class TestAssessment:
    def test_a_late_concession_closes_as_rejection_stands(self):
        """(a) The reviewer stopped listening 60 min after its rejection."""
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)]

        payload = _assess(transcript)

        assert payload is not None
        assert payload["reason"] == REJECTION_STANDS
        assert payload["termination_basis"] == REJECTION_STANDS
        assert payload["last_word_basis"] == BASIS_WINDOW_ENDED
        assert payload["verdict_acceptance"] == ACCEPTED
        assert payload["verdict_agent_id"] == REVIEWER

    def test_b_window_still_open_changes_nothing(self):
        """(b) A reply inside the response window: the reviewer may still revise."""
        transcript = _base(20) + [_msg("synthesis", PAUSED, 0, agrees=True)]

        assert _assess(transcript) is None

    def test_b_timely_reply_waits_for_the_reviewers_whole_lifetime(self):
        """A timely reply is owed a reconsideration until the hard cap."""
        transcript = _base(40) + [_msg("synthesis", PAUSED, 30, agrees=True)]

        # 40 minutes after the first verdict: well inside the 3h lifetime.
        assert _assess(transcript) is None
        # Past the lifetime plus grace, with no reconsideration filed.
        later = NOW + timedelta(seconds=TOTAL_S + LAST_WORD_GRACE_S)
        payload = _assess(transcript, now=later)
        assert payload is not None
        assert payload["last_word_basis"] == BASIS_WINDOW_ENDED
        assert payload["verdict_acceptance"] == ACCEPTED

    def test_reviewer_reconsidered_and_still_rejects(self):
        """The reviewer answered the paused agent's reply: its last word."""
        transcript = _base(20) + [
            _msg("synthesis", PAUSED, 10, agrees=True),
            _msg("synthesis", REVIEWER, 0, agrees=False),
        ]

        # Basis is independent of reviewer kind and of the window.
        payload = _assess(transcript, orchestrated=False)

        assert payload is not None
        assert payload["last_word_basis"] == BASIS_REVIEWER_ANSWERED
        assert payload["answered_reply_agrees"] is True
        # Acceptance describes replies to the STANDING verdict: none yet.
        assert payload["verdict_acceptance"] == NO_REPLY

    def test_no_reply_closes_only_after_window_and_grace(self):
        inside = _base(WAIT_S / 60 + 1)  # one minute past the wait, inside grace
        assert _assess(inside) is None

        past = _base(WAIT_S / 60 + LAST_WORD_GRACE_S / 60 + 1)
        payload = _assess(past)
        assert payload is not None
        assert payload["verdict_acceptance"] == NO_REPLY
        assert payload["paused_agent_reply_count"] == 0

    def test_late_contest_records_contested(self):
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=False)]

        payload = _assess(transcript)

        assert payload["verdict_acceptance"] == CONTESTED

    def test_window_rule_needs_an_orchestrated_reviewer(self):
        """A human reviewer declares no window, so none can have ended."""
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)]

        assert _assess(transcript, orchestrated=False) is None

    def test_only_when_awaiting_facilitation(self):
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)]

        assert _assess(transcript, awaiting=False) is None

    def test_no_reviewer_answer_is_out_of_scope(self):
        transcript = [
            _msg("thesis", PAUSED, 300),
            _msg("antithesis", REVIEWER, 290),
        ]

        assert _assess(transcript) is None

    def test_self_review_is_out_of_scope(self):
        transcript = [
            _msg("thesis", PAUSED, 300),
            _msg("antithesis", PAUSED, 290),
            _msg("synthesis", PAUSED, 280, agrees=False),
            _msg("synthesis", PAUSED, 0, agrees=True),
        ]

        assert _assess(transcript, reviewer=PAUSED) is None

    def test_reviewer_first_verdict_after_a_pre_verdict_reply_is_not_last_word(self):
        """A paused synthesis BEFORE any reviewer verdict was not answered as a
        reconsideration; the agent never got to respond to the rejection."""
        transcript = [
            _msg("thesis", PAUSED, 30),
            _msg("antithesis", REVIEWER, 25),
            _msg("synthesis", PAUSED, 20, agrees=False),
            _msg("synthesis", REVIEWER, 10, agrees=False),
        ]

        assert _assess(transcript) is None

    def test_a_replaced_reviewers_rejection_is_not_the_last_word(self):
        """After an operator reassign the new reviewer has not spoken yet and
        still holds authority to revise; the old rejection must not close it."""
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)]

        assert _assess(transcript, reviewer="agent-replacement") is None

    def test_reply_to_a_replaced_reviewer_is_not_answered_by_the_new_one(self):
        """Reviewer A rejects, the agent replies to A, the operator reassigns to
        B, and B's FIRST verdict rejects. B did not reconsider a reply to itself,
        so the answered basis must not fire; B's own window governs."""
        new = "agent-replacement"
        transcript = _base(30) + [
            _msg("synthesis", PAUSED, 20, agrees=True),
            _msg("synthesis", new, 5, agrees=False),
        ]

        # Inside B's window: stays open.
        assert _assess(transcript, reviewer=new) is None
        # Past B's window: closes on the window basis, never the answered basis.
        later = NOW + timedelta(seconds=WAIT_S + LAST_WORD_GRACE_S + 60)
        payload = _assess(transcript, reviewer=new, now=later)
        assert payload is not None
        assert payload["last_word_basis"] == BASIS_WINDOW_ENDED

    def test_unreadable_clock_stays_open(self):
        transcript = _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)]
        transcript[2] = DialecticMessage(
            phase="synthesis", agent_id=REVIEWER, timestamp="not a time", agrees=False
        )

        assert _assess(transcript) is None

    def test_db_shaped_rows_are_read(self):
        """The sweeper passes message rows: message_type and datetime stamps."""
        rows = [
            {"message_type": "thesis", "agent_id": PAUSED, "agrees": None,
             "timestamp": NOW - timedelta(minutes=100)},
            {"message_type": "antithesis", "agent_id": REVIEWER, "agrees": None,
             "timestamp": NOW - timedelta(minutes=91)},
            {"message_type": "synthesis", "agent_id": REVIEWER, "agrees": False,
             "timestamp": NOW - timedelta(minutes=90)},
            {"message_type": "synthesis", "agent_id": PAUSED, "agrees": True,
             "timestamp": NOW - timedelta(minutes=1)},
        ]

        payload = _assess(rows)

        assert payload is not None
        assert payload["verdict_acceptance"] == ACCEPTED


class TestNeverInTheAgentsFavour:
    """(c) The exit closes failed over a rejection; it never approves."""

    @pytest.mark.parametrize("transcript", [
        _base(90) + [_msg("synthesis", PAUSED, 0, agrees=True)],
        _base(20) + [_msg("synthesis", PAUSED, 10, agrees=True),
                     _msg("synthesis", REVIEWER, 0, agrees=False)],
        _base(200),
    ])
    def test_payload_is_never_an_approval(self, transcript):
        payload = _assess(transcript)

        assert payload is not None
        assert payload["action"] == REJECTION_STANDS
        assert payload["action"] != "resume"
        assert payload["reviewer_approved"] is False
        assert payload["standing_verdict"] == "reject"

    def test_a_reviewer_approval_is_never_closed_over(self):
        """The reviewer changed its mind: that is a resolution, not this exit."""
        transcript = _base(200) + [_msg("synthesis", REVIEWER, 150, agrees=True)]

        assert _assess(transcript) is None


# ---------------------------------------------------------------------------
# (d) Classification of the closed row
# ---------------------------------------------------------------------------


class TestClassification:
    def test_closed_rejection_stands_is_a_standing_disagreement(self):
        """status=failed, flag cleared on close, last reviewer synthesis
        agrees=false: the transcript signal classifies it."""
        outcome = classify_outcome(
            "failed", False, "opus_2f1ecbdf", standing_rejection=True
        )

        assert outcome == UNRESOLVED_AWAITING_FACILITATION
        assert outcome not in (RESOLVED, FAILED)

    def test_closed_rejection_stands_moves_neither_term_of_the_rate(self):
        base = {RESOLVED: 3, FAILED: 1, UNRESOLVED_AWAITING_FACILITATION: 0}
        closed = dict(base, **{UNRESOLVED_AWAITING_FACILITATION: 5})

        assert resolution_rate(base) == resolution_rate(closed)


# ---------------------------------------------------------------------------
# The synthesis handler
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bound_caller():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS

    ACTIVE_SESSIONS.clear()
    with patch("src.mcp_handlers.dialectic.auth.caller_is_bound_as", return_value=True):
        yield
    ACTIVE_SESSIONS.clear()


def _mock_server():
    server = MagicMock()
    server.agent_metadata = {
        PAUSED: SimpleNamespace(status="active", label="P", api_key="k",
                                last_update=NOW.isoformat(), paused_at=None,
                                structured_id=None),
        REVIEWER: SimpleNamespace(status="active", label="R", api_key="k",
                                  last_update=NOW.isoformat(), paused_at=None,
                                  structured_id=None),
    }
    server.monitors = {}
    server.load_metadata_async = AsyncMock()
    server.project_root = str(project_root)
    return server


def _live_session(rejected_minutes_ago, *, awaiting=False, extra=()):
    """A session at SYNTHESIS with real wall-clock stamps (the handler reads now)."""
    now = datetime.now(timezone.utc)

    def at(minutes):
        return (now - timedelta(minutes=minutes)).isoformat()

    s = DialecticSession(paused_agent_id=PAUSED, reviewer_agent_id=REVIEWER,
                         session_type="review")
    s.phase = DialecticPhase.SYNTHESIS
    s.synthesis_round = 1
    s.transcript.extend([
        DialecticMessage(phase="thesis", agent_id=PAUSED,
                         timestamp=at(rejected_minutes_ago + 10),
                         root_cause="cause", reasoning="r"),
        DialecticMessage(phase="antithesis", agent_id=REVIEWER,
                         timestamp=at(rejected_minutes_ago + 1), reasoning="r",
                         observed_metrics=ORCHESTRATED_STAMP),
        DialecticMessage(phase="synthesis", agent_id=REVIEWER,
                         timestamp=at(rejected_minutes_ago), reasoning="no",
                         proposed_conditions=["evidence"], agrees=False),
    ])
    for phase, agent, minutes, agrees in extra:
        s.transcript.append(DialecticMessage(
            phase=phase, agent_id=agent, timestamp=at(minutes), reasoning="r",
            proposed_conditions=["c"], agrees=agrees,
        ))
    s.awaiting_facilitation = awaiting
    return s


async def _submit(session, agent_id, agrees, *, beam_result=None, resolve_ok=True):
    from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

    pg_resolve = AsyncMock(return_value=resolve_ok)
    beam = AsyncMock(return_value=beam_result)
    flag = AsyncMock()
    execute = AsyncMock()
    server = _mock_server()
    with patch(f"{DIALECTIC}.mcp_server", server), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
         patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
         patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_update_phase", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.beam_update_phase", new_callable=AsyncMock, return_value=None), \
         patch(f"{DIALECTIC}.save_session", new_callable=AsyncMock), \
         patch(f"{DIALECTIC}.pg_update_awaiting_facilitation", flag), \
         patch(f"{DIALECTIC}.pg_resolve_session", pg_resolve), \
         patch(f"{DIALECTIC}.beam_resolve", beam), \
         patch(f"{DIALECTIC}.execute_resolution", execute), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
        result = await handle_submit_synthesis({
            "session_id": session.session_id,
            "agent_id": agent_id,
            "proposed_conditions": ["c"],
            "root_cause": "cause",
            "reasoning": "reply",
            "agrees": agrees,
            "api_key": "k",
        })
    data = json.loads(result[0].text)
    return data, SimpleNamespace(pg_resolve=pg_resolve, beam=beam, flag=flag,
                                 execute=execute)


class TestSynthesisHandler:
    @pytest.mark.asyncio
    async def test_the_handler_routes_to_facilitation_and_writes_no_terminal(self):
        """The exit is the sweeper's: it compare-and-sets the row it assessed.

        The handler's terminal path goes through BEAM, whose resolve has no
        compare-and-set predicates and answers `origin="new"` even when its
        guarded row write found the row already terminal. So even a late
        concession, past the reviewer's window, is answered as before; the
        sweeper closes it within one cycle.
        """
        session = _live_session(90)

        data, m = await _submit(session, PAUSED, True)

        assert data["error_code"] == "SELF_CLEAR_REFUSED"
        assert session.phase == DialecticPhase.SYNTHESIS
        assert session.awaiting_facilitation is True
        m.pg_resolve.assert_not_awaited()
        m.beam.assert_not_awaited()
        m.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# The sweeper
# ---------------------------------------------------------------------------


def _row(session_id="s" * 16, awaiting=True, phase="synthesis"):
    return {
        "session_id": session_id,
        "paused_agent_id": PAUSED,
        "reviewer_agent_id": REVIEWER,
        "phase": phase,
        "status": "active",
        "awaiting_facilitation": awaiting,
        # Recent: the 2h stuck threshold must not gate this exit.
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _messages(reply_minutes_after_rejection=30, rejected_minutes_ago=300):
    now = datetime.now(timezone.utc)
    rejected = now - timedelta(minutes=rejected_minutes_ago)
    return [
        {"message_type": "thesis", "agent_id": PAUSED, "agrees": None,
         "timestamp": rejected - timedelta(minutes=5), "observed_metrics": None},
        {"message_type": "antithesis", "agent_id": REVIEWER, "agrees": None,
         "timestamp": rejected - timedelta(minutes=1),
         "observed_metrics": json.dumps(ORCHESTRATED_STAMP)},
        {"message_type": "synthesis", "agent_id": REVIEWER, "agrees": False,
         "timestamp": rejected, "observed_metrics": None},
        {"message_type": "synthesis", "agent_id": PAUSED, "agrees": True,
         "timestamp": rejected + timedelta(minutes=reply_minutes_after_rejection),
         "observed_metrics": None},
    ]


ROW_STAMP = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def _session_row(messages, awaiting=True):
    """What get_session_async returns: the row's columns plus its messages."""
    return {"session_id": "s" * 16, "phase": "synthesis", "updated_at": ROW_STAMP,
            "paused_agent_id": PAUSED, "reviewer_agent_id": REVIEWER,
            "awaiting_facilitation": awaiting,
            "messages": messages}


@pytest.fixture
def _sweeper_io():
    with patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO_RESOLVE}.probe_saga_since_async",
               new_callable=AsyncMock, return_value={}), \
         patch(f"{AUTO_RESOLVE}.emit_guarded_write", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.emit_sweep_cycle", new_callable=AsyncMock):
        yield


class TestSweeper:
    @pytest.mark.asyncio
    async def test_declared_author_candidate_never_closes_rejection(
        self, _sweeper_io,
    ):
        from src.dialectic_reviewed_work import (
            declaration_for_request,
            embed_reviewed_work_authorship,
        )
        from src.mcp_handlers.dialectic.auto_resolve import (
            _auto_resolve_stuck_sessions,
        )

        paused = "11111111-1111-4111-8111-111111111111"
        author = "22222222-2222-4222-8222-222222222222"
        state = embed_reviewed_work_authorship(
            {}, declaration_for_request([author], declared_by_agent_id=paused)
        )
        candidate = _row()
        candidate.update({
            "paused_agent_id": paused,
            "reviewer_agent_id": author,
            "paused_agent_state_json": state,
        })
        read = AsyncMock()
        write = AsyncMock()
        with patch(
            f"{AUTO_RESOLVE}.get_active_sessions_async",
            new_callable=AsyncMock,
            return_value=[candidate],
        ), patch(f"{AUTO_RESOLVE}.get_session_async", read), patch(
            f"{AUTO_RESOLVE}.resolve_session_async", write
        ):
            result = await _auto_resolve_stuck_sessions()

        read.assert_not_awaited()
        write.assert_not_awaited()
        assert result["resolved_count"] == 0

    @pytest.mark.asyncio
    async def test_fresh_authorship_conflict_stops_close_after_candidate_read(
        self, _sweeper_io,
    ):
        from src.dialectic_reviewed_work import (
            declaration_for_request,
            embed_reviewed_work_authorship,
        )
        from src.mcp_handlers.dialectic.auto_resolve import (
            _auto_resolve_stuck_sessions,
        )

        paused = "11111111-1111-4111-8111-111111111111"
        author = "22222222-2222-4222-8222-222222222222"
        state = embed_reviewed_work_authorship(
            {}, declaration_for_request([author], declared_by_agent_id=paused)
        )
        candidate = _row()
        candidate.update({
            "paused_agent_id": paused,
            "reviewer_agent_id": author,
        })
        fresh = _session_row(_messages())
        fresh.update({
            "paused_agent_id": paused,
            "reviewer_agent_id": author,
            "paused_agent_state": state,
        })
        write = AsyncMock()
        with patch(
            f"{AUTO_RESOLVE}.get_active_sessions_async",
            new_callable=AsyncMock,
            return_value=[candidate],
        ), patch(
            f"{AUTO_RESOLVE}.get_session_async",
            new_callable=AsyncMock,
            return_value=fresh,
        ), patch(f"{AUTO_RESOLVE}.resolve_session_async", write):
            result = await _auto_resolve_stuck_sessions()

        write.assert_not_awaited()
        assert result["resolved_count"] == 0

    @pytest.mark.asyncio
    async def test_reviewer_never_answered_a_timely_reply_closes_after_lifetime(
        self, _sweeper_io,
    ):
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        write = AsyncMock(return_value=True)
        clear = AsyncMock(return_value=True)
        reap = AsyncMock(return_value=True)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(_messages())), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write), \
             patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async", clear), \
             patch(f"{AUTO_RESOLVE}.update_session_status_async", reap):
            result = await _auto_resolve_stuck_sessions()

        write.assert_awaited_once()
        # Compare-and-set against the row the assessment read.
        assert write.await_args.kwargs["expected_phase"] == "synthesis"
        assert write.await_args.kwargs["expected_updated_at"] == ROW_STAMP
        assert write.await_args.kwargs["expected_reviewer"] == REVIEWER
        assert write.await_args.kwargs["clear_facilitation"] is True
        payload = write.await_args.args[1]
        assert payload["reason"] == REJECTION_STANDS
        assert payload["closed_by"] == "python_sweeper"
        assert payload["verdict_acceptance"] == ACCEPTED
        clear.assert_not_awaited()  # cleared inside the terminal statement
        reap.assert_not_awaited()
        assert result["resolved_count"] == 1
        assert result["write_attempt_count"] == result["write_succeeded_count"] == 1
        assert any(d.get("reason") == REJECTION_STANDS for d in result["details"])

    @pytest.mark.asyncio
    async def test_flag_cleared_between_reads_stops_the_close(self, _sweeper_io):
        """The candidate list said awaiting, but the re-read row no longer is:
        someone acted in between, so the sweep must not close it."""
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        write = AsyncMock(return_value=True)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(_messages(), awaiting=False)), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write), \
             patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async",
                   new_callable=AsyncMock), \
             patch(f"{AUTO_RESOLVE}.update_session_status_async", new_callable=AsyncMock):
            await _auto_resolve_stuck_sessions()

        assert not any(
            c.args[1].get("reason") == REJECTION_STANDS
            for c in write.await_args_list if len(c.args) > 1
        )

    @pytest.mark.asyncio
    async def test_a_late_concession_closes_within_one_sweep(self, _sweeper_io):
        """(a) through the writer: the reply came after the reviewer stopped
        listening, and the row was touched minutes ago (no 2h idle needed)."""
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        write = AsyncMock(return_value=True)
        late = _messages(reply_minutes_after_rejection=90, rejected_minutes_ago=100)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(late)), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write), \
             patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async",
                   new_callable=AsyncMock):
            result = await _auto_resolve_stuck_sessions()

        assert write.await_args.args[2] == "failed"
        assert write.await_args.args[1]["verdict_acceptance"] == ACCEPTED
        assert result["resolved_count"] == 1

    @pytest.mark.asyncio
    async def test_reviewer_still_rejecting_a_reply_closes(self, _sweeper_io):
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        now = datetime.now(timezone.utc)
        msgs = _messages(reply_minutes_after_rejection=5, rejected_minutes_ago=20)
        msgs.append({"message_type": "synthesis", "agent_id": REVIEWER, "agrees": False,
                     "timestamp": now - timedelta(minutes=2), "observed_metrics": None})
        write = AsyncMock(return_value=True)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(msgs)), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write), \
             patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async",
                   new_callable=AsyncMock):
            await _auto_resolve_stuck_sessions()

        assert write.await_args.args[1]["last_word_basis"] == BASIS_REVIEWER_ANSWERED

    @pytest.mark.asyncio
    async def test_reviewer_still_inside_its_lifetime_is_left_open(self, _sweeper_io):
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        write = AsyncMock(return_value=True)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(_messages(rejected_minutes_ago=60))), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write):
            result = await _auto_resolve_stuck_sessions()

        write.assert_not_awaited()
        assert result["resolved_count"] == 0

    @pytest.mark.asyncio
    async def test_rows_not_awaiting_facilitation_are_not_read(self, _sweeper_io):
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        read = AsyncMock(return_value=_session_row(_messages()))
        write = AsyncMock(return_value=True)
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row(awaiting=False)]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", read), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", write):
            await _auto_resolve_stuck_sessions()

        read.assert_not_awaited()
        write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_close_is_not_counted(self, _sweeper_io):
        from src.mcp_handlers.dialectic.auto_resolve import _auto_resolve_stuck_sessions

        async def refused(session_id, payload, status, *, winner, **expected):
            winner.update(winner_status="failed", winner_reason=None, row_missing=False)
            return False

        clear = AsyncMock()
        with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
                   new_callable=AsyncMock, return_value=[_row()]), \
             patch(f"{AUTO_RESOLVE}.get_session_async", new_callable=AsyncMock,
                   return_value=_session_row(_messages())), \
             patch(f"{AUTO_RESOLVE}.resolve_session_async", refused), \
             patch(f"{AUTO_RESOLVE}.emit_write_refused", new_callable=AsyncMock), \
             patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async", clear):
            result = await _auto_resolve_stuck_sessions()

        assert result["resolved_count"] == 0
        assert result["skipped_count"] == 1
        clear.assert_not_awaited()


# ---------------------------------------------------------------------------
# The sweeper's DB write
# ---------------------------------------------------------------------------


class TestResolveSessionWinner:
    @pytest.mark.asyncio
    async def test_true_only_when_this_call_wrote(self):
        from src import dialectic_db

        async def wrote(session_id, resolution, status, *, detail):
            detail.update(written=True, effect_ts="t")
            return True

        db = SimpleNamespace(resolve_session=wrote)
        winner = {}
        with patch.object(dialectic_db, "get_dialectic_db", AsyncMock(return_value=db)):
            assert await dialectic_db.resolve_session_async(
                "s" * 16, {"reason": REJECTION_STANDS}, "failed", winner=winner
            ) is True
        assert winner["effect_ts"] == "t"

    @pytest.mark.asyncio
    async def test_an_already_failed_row_is_a_refusal_not_a_write(self):
        """resolve_session answers True for an idempotent replay; this must not."""
        from src import dialectic_db

        async def already(session_id, resolution, status, *, detail):
            detail.update(written=False, existing_status="failed")
            return True

        db = SimpleNamespace(resolve_session=already)
        winner = {}
        with patch.object(dialectic_db, "get_dialectic_db", AsyncMock(return_value=db)):
            assert await dialectic_db.resolve_session_async(
                "s" * 16, {"reason": REJECTION_STANDS}, "failed", winner=winner
            ) is False
        assert winner["winner_status"] == "failed"

    @pytest.mark.asyncio
    async def test_without_winner_the_idempotent_answer_is_unchanged(self):
        from src import dialectic_db

        async def already(session_id, resolution, status, *, detail):
            detail.update(written=False, existing_status="failed")
            return True

        db = SimpleNamespace(resolve_session=already)
        with patch.object(dialectic_db, "get_dialectic_db", AsyncMock(return_value=db)):
            assert await dialectic_db.resolve_session_async(
                "s" * 16, {"reason": "x"}, "failed"
            ) is True
