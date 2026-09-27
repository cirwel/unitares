"""The Wave 3 collision report's pre-registered definitions, on fixture rows.

The report is the read the gate's §7 window is judged on, so its definitions
are pinned here the way a stop rule is: the completeness check that decides
whether there is a reading at all, each class and the precedence between them,
all three harm orderings, same-value overlaps, and the heartbeat's three kinds
of gap. No test touches a database; ``analyze`` is pure over fetched rows.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wave3_collision_report", REPO_ROOT / "scripts" / "ops" / "wave3_collision_report.py"
)
report = importlib.util.module_from_spec(SPEC)
sys.modules["wave3_collision_report"] = report
SPEC.loader.exec_module(report)

T0 = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
SINCE = T0 - dt.timedelta(days=1)
UNTIL = T0 + dt.timedelta(days=1)


def at(minutes: float = 0, *, hours: float = 0) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes, hours=hours)


def write(session_id="s1", *, outcome="succeeded", attempted="reap_failed",
          read_at=None, commit_at=None, read_reviewer="rev-1", new_reviewer=None,
          winner_status=None, winner_reason=None, cycle_id="c1", probe="clean",
          attempt_id=None):
    read_at = read_at or at(-1)
    commit_at = commit_at or at(0)
    mutation = {"reap_failed": "reap", "awaiting_facilitation": "facilitation",
                "reviewer_reassignment": "reassignment"}[attempted]
    intended = "failed" if attempted == "reap_failed" else "active"
    return {
        "ts": commit_at,
        "session_id": session_id,
        "payload": {
            "session_id": session_id, "attempted": attempted, "mutation": mutation,
            "intended_status": intended, "outcome": outcome,
            "probe_outcome": probe if outcome == "succeeded" else None,
            "winner_status": winner_status, "winner_reason": winner_reason,
            "paused_agent_id": "paused-1", "new_reviewer_agent_id": new_reviewer,
            "read_reviewer_agent_id": read_reviewer, "read_phase": "antithesis",
            "read_status": "active",
            "decision_read_ts": read_at.isoformat(),
            "early_check_ts": (read_at + dt.timedelta(seconds=1)).isoformat(),
            "commit_ts": commit_at.isoformat(), "cycle_id": cycle_id,
            "attempt_id": attempt_id or f"gw-{session_id}-{commit_at.isoformat()}",
        },
    }


def session_row(session_id="s1", *, status="failed", reviewer="rev-1",
                resolution_json=None, updated_at=None):
    return {"session_id": session_id, "status": status, "phase": status,
            "reviewer_agent_id": reviewer, "paused_agent_id": "paused-1",
            "updated_at": updated_at or at(0), "resolution_reason": None,
            "resolution_json": resolution_json}


def message(ts, *, session_id="s1", agent="paused-1", kind="synthesis"):
    return {"session_id": session_id, "agent_id": agent, "message_type": kind, "timestamp": ts}


def saga(created, committed, *, session_id="s1", state="pg_committed", reviewer="rev-1",
         saga_id="sg-1", reason=None):
    return {"saga_id": saga_id, "session_id": session_id, "paused_agent_id": "paused-1",
            "reviewer_agent_id": reviewer, "state": state, "created_at": created,
            "pg_committed_at": committed if state == "pg_committed" else None,
            "reverted_at": None, "updated_at": committed, "payload_reason": reason}


_ATT = iter(range(10_000))


def session_write(attempt_ts, response_ts, *, kind, outcome="written", via="python",
                  requested=None, session_id="s1", decision_ts=None, attempt_id=None,
                  **response):
    """An attempt/response pair of `dialectic_session_write` rows."""
    aid = attempt_id or f"att-{next(_ATT)}"
    base = {"session_id": session_id, "attempt_id": aid, "kind": kind, "via": via,
            "site": None, "requested": requested,
            "decision_ts": decision_ts.isoformat() if decision_ts else None}
    rows = []
    if attempt_ts is not None:
        rows.append({"ts": attempt_ts, "event_type": "dialectic_session_write",
                     "session_id": session_id, "payload": {**base, "stage": "attempt"}})
    if response_ts is not None:
        rows.append({"ts": response_ts, "event_type": "dialectic_session_write",
                     "session_id": session_id,
                     "payload": {**base, "stage": "response", "outcome": outcome, **response}})
    return rows


def cycle(ts, seq, *, boot="boot-a", source="periodic", error=None, cycle_id=None,
          attempts=0, succeeded=0, refused=0, errors=0, clean=0, detected=0, failed=0,
          commit="abc123", emit_failures=0):
    return {"ts": ts, "payload": {
        "trigger_source": source, "process_boot_id": boot, "cycle_seq": seq,
        "cycle_id": cycle_id, "instrument_version": "wave3-instrument-v2",
        "code_commit": commit, "code_commit_source": "git", "error": error,
        "write_attempt_count": attempts, "write_succeeded_count": succeeded,
        "write_refused_count": refused, "write_error_count": errors,
        "overlap_clean_count": clean, "overlap_detected_count": detected,
        "overlap_probe_failed_count": failed,
        "emit_failures_since_last_cycle": emit_failures,
    }}


def run(writes, *, sessions=(), messages=(), sagas=(), events=(), cycles=None,
        window_sagas=(), correlation_hours=6.0):
    if cycles is None:
        # A matching periodic v2 cycle row, so the reading can be complete.
        mine = [w["payload"] for w in writes if w["payload"].get("cycle_id") == "c1"]
        n = {o: sum(1 for w in mine if w["outcome"] == o)
             for o in ("succeeded", "refused", "error")}
        # Before the writes: the window starts at the first periodic v2 row.
        cycles = [cycle(SINCE + dt.timedelta(minutes=1), 1, cycle_id="c1",
                        attempts=len(mine),
                        succeeded=n["succeeded"], refused=n["refused"], errors=n["error"],
                        clean=n["succeeded"])]
    return report.analyze(
        writes=list(writes), cycles=list(cycles), competing_events=list(events),
        sessions=list(sessions) or [session_row()], messages=list(messages),
        sagas=list(sagas), mismatches=[], inferred_python_terminal=[],
        since=SINCE, until=UNTIL, window_sagas=list(window_sagas),
        correlation_hours=correlation_hours,
    )


def only(result):
    assert len(result["writes"]) == 1
    return result["writes"][0]


class TestHarmA_Contradicted:
    """In flight across the sweeper's commit, and defeated by it."""

    def test_a_resolve_answered_already_terminal_after_the_reap_is_harm(self):
        w = only(run([write()], events=session_write(
            at(-0.5), at(0.5), kind="resolve", via="beam", requested="resolved",
            outcome="already_terminal", decision_ts=at(-0.5))))
        assert w["class"] == "harm"
        assert w["evidence"]["harm_a"][0]["kind"] == "session_write:resolve"

    def test_a_same_value_resolve_is_benign_not_harm(self):
        """Both intended `failed`: no adverse consequence."""
        w = only(run([write()], events=session_write(
            at(-0.5), at(0.5), kind="resolve", via="python_fallback", requested="failed",
            outcome="not_written", decision_ts=at(-0.5))))
        assert w["class"] == "contention_benign"

    def test_a_straddling_saga_is_harm_unless_it_is_liveness(self):
        assert only(run([write()], sagas=[saga(at(-0.5), at(0.5))]))["class"] == "harm"
        liveness = saga(at(-0.5), at(0.5), reason="liveness_timeout")
        assert only(run([write()], sagas=[liveness]))["class"] == "contention_benign"

    def test_the_decision_time_is_the_cause(self):
        """A write decided before the commit but attempted after it straddles."""
        w = only(run([write(read_at=at(-10))], events=session_write(
            at(1), at(2), kind="reviewer", via="beam", requested="rev-9",
            outcome="not_written", decision_ts=at(-0.2))))
        assert w["class"] == "harm"

    def test_outside_the_correlation_bound_is_not_harm(self):
        w = only(run([write()], sagas=[saga(at(hours=-7), at(1))]))
        assert w["class"] != "harm"

    def test_the_bound_is_a_flag(self):
        w = only(run([write()], sagas=[saga(at(hours=-7), at(1))], correlation_hours=8))
        assert w["class"] == "harm"


class TestHarmB_StaleDecision:
    """A competing write took effect between the sweeper's read and its commit."""

    def test_a_protocol_message_after_the_read_is_harm(self):
        w = only(run([write(read_at=at(-5))], messages=[message(at(-2))]))
        assert w["class"] == "harm"
        assert w["evidence"]["harm_b"][0]["kind"] == "protocol_message"

    def test_any_message_moves_the_staleness_clock(self):
        """A system message another writer appended is still ordering (b)."""
        w = only(run([write(read_at=at(-5))], messages=[message(at(-2), agent="system")]))
        assert w["class"] == "harm"

    def test_harm_b_wins_even_when_the_final_status_is_the_intended_one(self):
        w = only(run(
            [write(read_at=at(-5))],
            sessions=[session_row(status="failed")],
            messages=[message(at(-2))],
            events=session_write(at(30), at(31), kind="resolve", via="beam",
                                 requested="failed", outcome="already_terminal"),
        ))
        assert w["final_status"] == "failed" == w["intended_status"]
        assert w["class"] == "harm"

    def test_a_same_value_write_after_the_read_is_benign(self):
        """Both set awaiting_facilitation=true: a same-value overlap."""
        w = only(run([write(attempted="awaiting_facilitation", read_at=at(-5))],
                     sessions=[session_row(status="active")],
                     events=session_write(at(-3), at(-2), kind="facilitation",
                                          requested=True)))
        assert w["class"] == "contention_benign"

    def test_update_reviewer_just_before_a_reap_is_harm_from_the_row_alone(self):
        """A reviewer write is refused on a terminal row, so a reaped row
        naming a reviewer the sweeper did not read was written between the read
        and the reap, with no event of its own."""
        w = only(run([write(read_reviewer="rev-1")],
                     sessions=[session_row(status="failed", reviewer="rev-2",
                                           updated_at=at(hours=2))]))
        assert w["class"] == "harm"
        assert w["evidence"]["harm_b"][0]["evidence"] == "session_row"

    def test_row_drift_is_not_placed_when_another_writer_finished_the_row(self):
        w = only(run([write(read_reviewer="rev-1")],
                     sessions=[session_row(status="failed", reviewer="rev-2",
                                           resolution_json={"reason": "x"},
                                           updated_at=at(hours=2))]))
        assert w["class"] != "harm"
        assert w["evidence"]["adjudicate"]

    def test_row_drift_is_placed_when_the_last_write_bounds_it(self):
        w = only(run([write(read_reviewer="rev-1")],
                     sessions=[session_row(status="failed", reviewer="rev-2",
                                           resolution_json={"reason": "x"},
                                           updated_at=at(0))]))
        assert w["class"] == "harm"

    def test_row_drift_is_not_placed_for_an_earlier_reap(self):
        result = run([write(commit_at=at(0), read_at=at(-1)),
                      write(commit_at=at(hours=10),
                            read_at=at(hours=10) - dt.timedelta(minutes=1),
                            read_reviewer="rev-2")],
                     sessions=[session_row(status="failed", reviewer="rev-2",
                                           updated_at=at(hours=10))])
        first = [w for w in result["writes"] if w["commit_ts"] == at(0).isoformat()][0]
        assert first["class"] != "harm"


class TestHarmC_Reversed:
    """After a reap, a competing write that landed and invalidates it."""

    def test_a_message_appended_to_the_failed_session_is_harm(self):
        w = only(run([write()], messages=[message(at(10))]))
        assert w["class"] == "harm"
        assert w["evidence"]["harm_reverse"][0]["kind"] == "protocol_message"

    def test_a_reopen_after_the_reap_is_harm_even_when_it_ends_failed_again(self):
        w = only(run([write()], sessions=[session_row(status="failed")],
                     events=session_write(at(10), at(11), kind="reopen",
                                          requested="antithesis")))
        assert w["class"] == "harm"

    def test_the_sweepers_own_transcript_line_is_not_reverse_harm(self):
        w = only(run([write()], messages=[message(at(0.01), agent="system")]))
        assert w["class"] == "uncontended"

    def test_activity_after_a_facilitation_request_is_its_intended_outcome(self):
        w = only(run([write(attempted="awaiting_facilitation")],
                     sessions=[session_row(status="resolved")],
                     messages=[message(at(30))], sagas=[saga(at(40), at(41))]))
        assert w["class"] == "uncontended"


class TestContention:
    def test_refused_with_the_intended_status_is_benign(self):
        w = only(run([write(outcome="refused", winner_status="failed",
                            winner_reason="liveness_timeout")]))
        assert w["class"] == "contention_benign"

    def test_refused_with_another_status_is_divergent(self):
        w = only(run([write(outcome="refused", winner_status="resolved")]))
        assert w["class"] == "contention_divergent"

    def test_a_refused_facilitation_request_is_divergent_on_any_terminal_winner(self):
        w = only(run([write(outcome="refused", attempted="awaiting_facilitation",
                            winner_status="failed")]))
        assert w["class"] == "contention_divergent"

    def test_a_non_terminal_winner_read_is_unattributed(self):
        """The refusal read raced a reopen: it does not name the winner."""
        w = only(run([write(outcome="refused", winner_status="active")]))
        assert w["class"] == "contention_unattributed"

    def test_a_reopen_around_the_refusal_makes_the_winner_unattributed(self):
        """The follow-up read may describe a later transition, not the refusal's."""
        w = only(run([write(outcome="refused", winner_status="failed")],
                     events=session_write(at(-0.5), at(0.001), kind="reopen",
                                          requested="antithesis")))
        assert w["class"] == "contention_unattributed"

    def test_message_session_writes_are_not_double_counted(self):
        """A message is observed from its table row; its bracket adds nothing."""
        w = only(run([write(attempted="awaiting_facilitation")],
                     sessions=[session_row(status="active")],
                     messages=[message(at(0.3))],
                     events=session_write(at(-0.2), at(0.4), kind="message",
                                          requested="synthesis")))
        assert w["class"] != "ambiguous"

    def test_an_uncertain_competing_outcome_is_ambiguous_not_clean(self):
        for outcome in ("no_response", "error", "interrupted", "accepted_effect_unknown"):
            w = only(run([write()], events=session_write(
                at(1), at(1.1), kind="phase", via="beam", requested="synthesis",
                outcome=outcome)))
            assert w["class"] == "ambiguous", outcome

    def test_a_duplicate_create_is_not_a_defeated_write(self):
        w = only(run([write()], events=session_write(
            at(-0.5), at(0.5), kind="create", outcome="not_written")))
        assert w["class"] != "harm"

    def test_refused_without_a_recorded_winner_is_unattributed(self):
        w = only(run([write(outcome="refused", winner_status=None)]))
        assert w["class"] == "contention_unattributed"

    def test_a_refused_write_is_never_harm(self):
        w = only(run([write(outcome="refused", winner_status="resolved", read_at=at(-5))],
                     messages=[message(at(-2))], sagas=[saga(at(-0.5), at(0.5))]))
        assert w["class"] == "contention_divergent"

    def test_a_later_terminal_writer_after_a_reap_is_contention(self):
        w = only(run([write()], events=session_write(
            at(12), at(13), kind="resolve", via="beam", requested="failed",
            outcome="already_terminal", decision_ts=at(11))))
        assert w["class"] == "contention_benign"

    def test_a_later_terminal_writer_whose_outcome_differs_is_divergent(self):
        w = only(run([write()], sessions=[session_row(status="resolved")],
                     events=session_write(at(12), at(13), kind="resolve",
                                          requested="resolved", decision_ts=at(11))))
        assert w["class"] == "contention_divergent"

    def test_an_errored_write_has_an_unknown_outcome(self):
        w = only(run([write(outcome="error")]))
        assert w["class"] == "unknown_outcome"

    def test_the_sweepers_own_session_writes_are_not_competing(self):
        w = only(run([write(read_at=at(-5))], events=session_write(
            at(-0.1), at(0), kind="status", via="sweeper", requested="failed")))
        assert w["class"] == "uncontended"


class TestCompleteness:
    def test_a_fully_matched_window_is_complete(self):
        result = run([write()], events=session_write(at(30), at(31), kind="phase"))
        assert result["reading"] == "COMPLETE"
        assert result["counts_are_lower_bounds"] is False

    def test_an_attempt_without_a_response_is_inconclusive(self):
        result = run([write()], events=session_write(at(30), None, kind="phase"))
        assert result["reading"] == "INCONCLUSIVE"
        assert result["completeness"]["unmatched_session_writes"][0]["missing"] == "response"
        assert result["counts_are_lower_bounds"] is True

    def test_a_response_without_an_attempt_is_inconclusive(self):
        result = run([write()], events=session_write(None, at(31), kind="phase"))
        assert result["completeness"]["unmatched_session_writes"][0]["missing"] == "attempt"
        assert result["reading"] == "INCONCLUSIVE"

    def test_guarded_rows_must_equal_the_cycle_count(self):
        result = run([write()], cycles=[cycle(at(-0.5), 1, cycle_id="c1", attempts=2)])
        assert result["reading"] == "INCONCLUSIVE"
        mismatch = result["completeness"]["cycles_whose_rows_do_not_match"][0]
        assert (mismatch["write_attempt_count"], mismatch["guarded_write_rows"]) == (2, 1)
        assert mismatch["process_boot_id"] == "boot-a" and mismatch["cycle_seq"] == 1

    def test_a_guarded_row_without_its_cycle_row_is_inconclusive(self):
        result = run([write(cycle_id="lost")],
                     cycles=[cycle(at(-0.5), 1, cycle_id="c1", attempts=0)])
        assert result["completeness"]["guarded_write_rows_without_a_cycle"]
        assert result["reading"] == "INCONCLUSIVE"

    def test_an_unexplained_cycle_seq_gap_is_inconclusive(self):
        result = run([], cycles=[cycle(at(0), 1), cycle(at(10), 3)])
        assert result["completeness"]["cycle_seq_gaps"][0]["missing_cycle_seq"] == [2]
        assert result["reading"] == "INCONCLUSIVE"

    def test_a_seq_gap_explained_by_its_ledger_line_is_uncovered_not_inconclusive(self):
        ledger = {"path": "x", "status": "read", "lines": [
            {"ts": at(5).isoformat(), "event_type": "dialectic_sweep_cycle",
             "process_boot_id": "boot-a", "cycle_seq": 2}]}
        result = report.analyze(
            writes=[], cycles=[cycle(at(0), 1), cycle(at(10), 3)], competing_events=[],
            sessions=[], messages=[], sagas=[], mismatches=[], inferred_python_terminal=[],
            since=SINCE, until=UNTIL, emit_failure_ledger=ledger)
        assert result["completeness"]["explained_seq_gaps"][0]["missing_cycle_seq"] == [2]
        assert result["reading"] == "COMPLETE"

    def test_an_unnamed_committed_saga_is_inconclusive_but_liveness_is_not(self):
        live = {"saga_id": "live", "payload_reason": "liveness_timeout"}
        assert run([], window_sagas=[live])["reading"] == "COMPLETE"
        routed = {"saga_id": "sg-x", "payload_reason": None}
        assert run([], window_sagas=[routed])["reading"] == "INCONCLUSIVE"
        named = session_write(at(1), at(2), kind="resolve", via="beam",
                              requested="resolved", saga_id="sg-x")
        assert run([], window_sagas=[routed], events=named)["reading"] == "COMPLETE"

    def test_a_reported_emit_failure_uncovers_its_interval_and_excludes_its_writes(self):
        """Not permanently inconclusive: the interval from the previous cycle row
        to the reporting one is UNCOVERED, and the writes inside it are excluded."""
        cycles = [cycle(at(-10), 1, cycle_id="c0"),
                  cycle(at(0.5), 2, cycle_id="c1", attempts=1, succeeded=1, clean=1,
                        emit_failures=1),
                  cycle(at(10), 3, cycle_id="c2", attempts=1, succeeded=1, clean=1)]
        result = run([write(), write("s2", commit_at=at(9.5), read_at=at(9), cycle_id="c2")],
                     sessions=[session_row(), session_row("s2")], cycles=cycles)
        comp = result["completeness"]
        assert comp["uncovered_intervals"][0]["from"] == at(-10).isoformat()
        assert comp["uncovered_intervals"][0]["to"] == at(0.5).isoformat()
        assert [w["session_id"] for w in result["writes_excluded_as_uncovered"]] == ["s1"]
        assert [w["session_id"] for w in result["writes"]] == ["s2"]
        assert result["reading"] == "COMPLETE"

    def _with_ledger(self, ledger):
        return report.analyze(
            writes=[], cycles=[cycle(at(0), 1)], competing_events=[], sessions=[],
            messages=[], sagas=[], mismatches=[], inferred_python_terminal=[],
            since=SINCE, until=UNTIL, emit_failure_ledger=ledger)

    def test_a_ledger_line_in_the_window_uncovers_an_interval(self):
        ledger = {"path": "x", "status": "read", "lines": [
            {"ts": at(1).isoformat(), "event_type": "dialectic_guarded_write"},
            {"ts": (SINCE - dt.timedelta(days=3)).isoformat(), "event_type": "old"},
        ]}
        result = self._with_ledger(ledger)
        assert len(result["completeness"]["emit_failure_ledger"]["lines_in_window"]) == 1
        interval = result["completeness"]["uncovered_intervals"][0]
        assert interval["from"] == at(0).isoformat(), "the last cycle row before it"
        assert interval["to"] == UNTIL.isoformat(), "no cycle row after it: to the window end"
        assert result["reading"] == "COMPLETE"
        assert result["heartbeat"]["uncovered_minutes"] > 0

    def test_a_corrupt_ledger_line_cannot_be_placed_and_is_inconclusive(self):
        result = self._with_ledger({"path": "x", "status": "read",
                                    "lines": [{"ts": None, "raw": "{tor"}]})
        assert result["reading"] == "INCONCLUSIVE"

    def test_an_unreadable_ledger_is_inconclusive_but_an_absent_one_is_not(self):
        assert self._with_ledger({"path": "x", "status": "unreadable",
                                  "lines": []})["reading"] == "INCONCLUSIVE"
        assert self._with_ledger({"path": "x", "status": "absent",
                                  "lines": []})["reading"] == "COMPLETE"

    def test_the_ledger_reader(self, tmp_path):
        path = tmp_path / "ledger.jsonl"
        assert report.read_emit_failure_ledger(str(path))["status"] == "absent"
        path.write_text('{"ts": "2026-10-05T12:00:00+00:00"}\n{torn\n')
        read = report.read_emit_failure_ledger(str(path))
        assert read["status"] == "read" and len(read["lines"]) == 2
        assert read["lines"][1]["ts"] is None

    def test_an_unbalanced_cycle_row_is_inconclusive(self):
        """A missing probe must not become a clean zero."""
        cycles = [cycle(at(-0.5), 1, cycle_id="c1", attempts=1, succeeded=1, clean=0)]
        result = run([write()], cycles=cycles)
        assert result["completeness"]["unbalanced_cycle_rows"]
        assert result["reading"] == "INCONCLUSIVE"

    def test_the_window_starts_at_the_first_periodic_v2_row(self):
        """Writes before the instrument started are not in the reading."""
        cycles = [cycle(at(0.5), 1, cycle_id="c1", attempts=0)]
        result = run([write(commit_at=at(-30), read_at=at(-31), cycle_id="old")],
                     cycles=cycles)
        assert result["window"]["since"] == at(0.5).isoformat()
        assert result["window"]["requested_since"] == SINCE.isoformat()
        assert result["guarded_writes"] == 0
        assert "requested --since" in report.render_text(result)

    def test_a_saga_before_the_instrument_started_is_not_owed(self):
        early = {"saga_id": "sg-old", "payload_reason": None, "created_at": at(-30)}
        result = run([], cycles=[cycle(at(0), 1)], window_sagas=[early])
        assert result["reading"] == "COMPLETE"

    def test_no_instrument_rows_is_not_started_not_complete(self):
        result = run([], cycles=[])
        assert result["reading"] == "NOT_STARTED"
        assert result["counts_are_lower_bounds"] is True

    def test_an_inconclusive_reading_prints_lower_bounds_not_zeros(self):
        result = run([write()], events=session_write(at(30), None, kind="phase"))
        text = report.render_text(result)
        assert "READING: INCONCLUSIVE" in text
        assert "harm                       >=0" in text
        assert "UNMATCHED response missing" in text


class TestAmbiguous:
    """An effect known only as an interval that contains the sweeper's commit."""

    def test_a_beam_write_bracketing_the_commit_is_ambiguous(self):
        w = only(run([write()], events=session_write(
            at(-0.2), at(0.2), kind="reviewer", via="beam", requested="rev-9",
            decision_ts=at(-0.3))))
        assert w["class"] == "ambiguous"

    def test_ambiguous_is_inconclusive_and_never_harm(self):
        result = run([write()], events=session_write(
            at(-0.2), at(0.2), kind="reviewer", via="beam", requested="rev-9"))
        assert result["class_counts"]["harm"] == 0
        assert result["class_counts"]["ambiguous"] == 1
        assert result["reading"] == "INCONCLUSIVE"

    def test_definite_harm_on_the_same_write_is_still_harm(self):
        w = only(run([write(read_at=at(-5))], messages=[message(at(-2))],
                     events=session_write(at(-0.2), at(0.2), kind="reviewer", via="beam",
                                          requested="rev-9")))
        assert w["class"] == "harm"

    def test_a_same_value_bracketing_write_is_benign_not_ambiguous(self):
        w = only(run([write()], events=session_write(
            at(-0.2), at(0.2), kind="status", via="python", requested="failed",
            outcome="not_written")))
        assert w["class"] == "contention_benign"

    def test_a_db_clock_effect_time_removes_the_ambiguity(self):
        w = only(run([write()], events=session_write(
            at(-0.2), at(0.2), kind="reviewer", via="python", requested="rev-9",
            effect_ts=at(-0.1).isoformat())))
        assert w["class"] == "harm", "placed before the commit: a stale decision"


class TestHeartbeat:
    def _hb(self, cycles):
        return report.heartbeat(cycles, since=at(0), until=at(60),
                                silence=dt.timedelta(minutes=22))

    def test_a_missing_seq_is_a_lost_audit_write(self):
        hb = self._hb([cycle(at(0), 1), cycle(at(10), 2), cycle(at(20), 4)])
        assert hb["boots"][0]["missing_cycle_seq"] == [3]

    def test_lazy_rows_fill_the_sequence(self):
        hb = self._hb([cycle(at(0), 1), cycle(at(5), 2, source="active_session_check"),
                       cycle(at(10), 3)])
        assert hb["boots"][0]["missing_cycle_seq_count"] == 0

    def test_a_boot_change_is_a_restart(self):
        hb = self._hb([cycle(at(0), 1), cycle(at(10), 2),
                       cycle(at(40), 1, boot="boot-b"), cycle(at(50), 2, boot="boot-b")])
        assert len(hb["boots"]) == 2
        assert hb["restarts"][0]["minutes"] == 30.0
        assert hb["boots"][1]["missing_cycle_seq_count"] == 0, "seq restarts per boot"

    def test_an_in_boot_silence_is_a_hung_loop(self):
        hb = self._hb([cycle(at(0), 1), cycle(at(10), 2), cycle(at(45), 3)])
        silences = hb["boots"][0]["in_boot_silences"]
        assert len(silences) == 1 and silences[0]["minutes"] == 35.0

    def test_coverage_counts_only_gaps_within_the_bound(self):
        hb = self._hb([cycle(at(0), 1), cycle(at(10), 2), cycle(at(20), 3), cycle(at(60), 4)])
        assert hb["periodic_coverage"] == pytest.approx(20 / 60)

    def test_no_v2_rows_is_not_zero_coverage(self):
        hb = self._hb([{"ts": at(0), "payload": {"trigger_source": "periodic"}}])
        assert hb["periodic_coverage"] is None
        assert hb["legacy_rows_without_boot_id"] == 1
        assert hb["first_periodic_v2_row"] is None

    def test_the_window_start_record_is_the_first_periodic_v2_row_and_its_commit(self):
        hb = self._hb([cycle(at(5), 1, source="active_session_check", commit="lazy"),
                       cycle(at(10), 2, commit="deadbeef")])
        assert hb["first_periodic_v2_row"] == {
            "ts": at(10).isoformat(), "code_commit": "deadbeef", "code_commit_source": "git"}

    def test_unbalanced_rows_are_counted(self):
        hb = self._hb([cycle(at(0), 1, attempts=2, succeeded=1, refused=0, clean=1)])
        assert hb["boots"][0]["unbalanced_rows"] == 1

    def test_timeouts_are_counted_apart_from_other_errors(self):
        hb = self._hb([cycle(at(0), 1, error="timeout"), cycle(at(10), 2, error="boom")])
        assert hb["boots"][0]["timeouts"] == 1
        assert hb["boots"][0]["errors"] == 1


class TestOutput:
    def test_text_and_json_render(self):
        result = run([write(), write("s2", outcome="refused", winner_status="resolved")],
                     sessions=[session_row(), session_row("s2")])
        text = report.render_text(result)
        assert "Guarded sweeper writes: 2" in text
        assert "contention_divergent" in text
        assert "Writer inventory" in text
        assert "benign by construction" in text
        json.dumps(result, default=str)

    def test_every_inventory_writer_is_reported_with_how_it_is_observed(self):
        rows = run([])["writer_inventory"]
        assert rows and all(r["observed_via"] for r in rows)

    def test_class_counts_sum_to_the_writes(self):
        result = run([write("a"), write("b", outcome="refused", winner_status="failed"),
                      write("c", outcome="error"), write("d", read_at=at(-5))],
                     sessions=[session_row(s) for s in "abcd"],
                     messages=[message(at(-2), session_id="d")])
        assert sum(result["class_counts"].values()) == result["guarded_writes"] == 4
        assert result["class_counts"]["harm"] == 1


class TestReadOnlyAndBounded:
    def test_every_query_is_time_bounded_or_keyed(self):
        for name in ("EVENTS_QUERY", "MESSAGES_QUERY", "SAGAS_QUERY", "MISMATCH_QUERY",
                     "INFERRED_PY_TERMINAL_QUERY", "WINDOW_SAGAS_QUERY"):
            sql = getattr(report, name)
            assert "%(lo)s" in sql and "%(hi)s" in sql, name
        assert "ANY(%(session_ids)s)" in report.SESSIONS_QUERY

    def test_no_query_writes(self):
        for name in dir(report):
            if name.endswith("_QUERY"):
                sql = getattr(report, name).upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "DROP "):
                    assert verb not in sql, (name, verb)

    def test_the_connection_is_opened_read_only(self, monkeypatch):
        seen = {}

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                seen.setdefault("sql", []).append(sql)

            def fetchall(self):
                return []

        class Conn:
            def set_session(self, **kw):
                seen["session"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self, **kw):
                return Cursor()

            def close(self):
                seen["closed"] = True

        fake = types.ModuleType("psycopg2")
        fake.connect = lambda dsn, connect_timeout: Conn()
        extras = types.ModuleType("psycopg2.extras")
        extras.RealDictCursor = object
        fake.extras = extras
        monkeypatch.setitem(sys.modules, "psycopg2", fake)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)

        report._run_read_only("postgresql://x", [("SELECT 1", {})])
        assert seen["session"] == {"readonly": True}
        assert seen["closed"] is True
        assert "statement_timeout" in seen["sql"][0]

    def test_a_failed_query_exits_2_and_does_not_raise(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(report, "fetch", boom)
        assert report.main(["--since", "2026-10-01T00:00:00Z",
                            "--until", "2026-10-02T00:00:00Z"]) == 2
        assert "query failed" in capsys.readouterr().err

    def test_since_must_precede_until(self):
        assert report.main(["--since", "2026-10-02T00:00:00Z",
                            "--until", "2026-10-01T00:00:00Z"]) == 2
