"""The unresolved-review report must stay SILENT when there is nothing to say.

It feeds an operator SessionStart surface, and the house pattern there (see
scripts/hooks/worktree-backlog-surface.sh in fleet-ops) is that a surface which
prints on every start gets ignored within a week. Silence on an empty result is
therefore load-bearing, not cosmetic.

The other pinned property is that conditions are never elided quietly. The
conditions ARE the artifact this report exists to deliver; a list that silently
shows 4 of 9 reads as "there were 4".
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dialectic_unresolved", REPO_ROOT / "scripts" / "ops" / "dialectic_unresolved.py"
)
report = importlib.util.module_from_spec(SPEC)
sys.modules["dialectic_unresolved"] = report
SPEC.loader.exec_module(report)


@pytest.fixture(autouse=True)
def _no_live_superseders(monkeypatch):
    """main() makes a second read for supersession; never let a test reach a
    real database through it. Tests that exercise supersession override this."""
    monkeypatch.setattr(report, "fetch_superseders", lambda dsn, window_days: [])


def _row(**over):
    base = dict(
        session_id="abc123",
        topic="PR #99 - a thing that needed review",
        reviewer="DialecticReviewer_dead",
        awaiting_facilitation=False,
        status="failed",
        phase="failed",
        created_at=dt.datetime(2026, 9, 1, 12, 0),
        updated_at=dt.datetime(2026, 9, 3, 12, 0),
        reviewer_reasoning="the thesis did not establish X",
        conditions=["do X", "measure Y"],
    )
    base.update(over)
    return base


class TestSilence:
    def test_no_reviews_renders_nothing_at_all(self):
        """Not a header, not a count, not a newline. The hook prints what it gets."""
        assert report.render([], max_conditions=4) == ""

    def test_one_review_renders_something(self):
        assert report.render([_row()], max_conditions=4).strip()


class TestConditionsAreNeverElidedSilently:
    def test_truncation_says_how_many_it_dropped(self):
        out = report.render([_row(conditions=[f"c{i}" for i in range(9)])], max_conditions=4)
        assert "c0" in out and "c3" in out
        assert "c4" not in out.split("more condition")[0]
        assert "and 5 more condition(s)" in out

    def test_no_truncation_notice_when_all_shown(self):
        out = report.render([_row(conditions=["a", "b"])], max_conditions=4)
        assert "more condition" not in out

    def test_a_reviewer_who_objected_without_conditions_is_stated_not_blank(self):
        """An empty bullet list would read as 'no objection'. It is the opposite."""
        out = report.render([_row(conditions=[])], max_conditions=4)
        assert "proposed no conditions" in out


class TestRenderingIsRobust:
    def test_newlines_in_a_topic_do_not_break_the_block_layout(self):
        out = report.render([_row(topic="line one\nline two\nline three")], max_conditions=4)
        body = [ln for ln in out.split("\n") if "line one" in ln]
        assert len(body) == 1 and "line two" in body[0]

    def test_a_very_long_topic_is_truncated_with_an_ellipsis(self):
        out = report.render([_row(topic="x" * 400)], max_conditions=4)
        assert "..." in out
        assert max(len(ln) for ln in out.split("\n")) < 200

    def test_missing_topic_falls_back_rather_than_printing_none(self):
        out = report.render([_row(topic="")], max_conditions=4)
        assert "(no topic)" in out
        assert "None" not in out

    def test_every_session_id_appears(self):
        rows = [_row(session_id=f"s{i}") for i in range(5)]
        out = report.render(rows, max_conditions=4)
        for i in range(5):
            assert f"s{i}" in out

    def test_the_count_matches_the_rows(self):
        out = report.render([_row(), _row(session_id="two")], max_conditions=4)
        assert out.startswith("2 dialectic review(s)")


class TestQueryShape:
    """The SQL carries three measured decisions; pin them so an edit is deliberate."""

    def test_probe_exclusion_reuses_the_repo_frozen_rule(self):
        """⛔The rule is scripts/dev/dialectic_verdict_labels.PROBE_FAMILY_RE,
        whose comment says changing it requires a new cohort ID. A draft here
        filtered only canary_dialectic% and let 22 rate-probe sessions through,
        inflating the operator headline from 41 to 63."""
        assert "(probe|canary)" in report.QUERY
        assert "^RP[0-9]" in report.QUERY

    def test_the_exclusion_is_not_a_substring_test_for_test_or_demo(self):
        """`claude-federation-attestation` contains 'test' and is real work."""
        assert "'test'" not in report.QUERY and "demo" not in report.QUERY

    def test_canary_is_partitioned_on_label_not_trigger_source(self):
        assert "canary" in report.QUERY
        assert "trigger_source" not in report.QUERY, (
            "trigger_source has zero discriminating power here -- measured "
            "'manual' for all 51 canary sessions AND 127 non-canary ones"
        )

    def test_the_agents_join_is_a_left_join(self):
        """12 sessions have a paused_agent_id with no agents row; an inner join
        would drop real unresolved work while looking like a filter."""
        assert "LEFT JOIN core.agents pa" in report.QUERY

    def test_unresolved_is_not_status_active(self):
        """There are no active sessions at all, so keying on 'active' returns
        an empty list forever and reads as all-clear."""
        assert "status <> 'resolved'" in report.QUERY
        assert "status = 'active'" not in report.QUERY

    def test_only_a_standing_rejection_qualifies(self):
        assert "r.agrees IS FALSE" in report.QUERY
        assert "ORDER BY dm.timestamp DESC" in report.QUERY

    def test_the_query_carries_its_own_time_bound(self):
        """⛔The SessionStart hook cannot bound this: `timeout` is not installed
        on macOS, so an earlier shell guard silently did nothing on the only
        machine that runs it."""
        assert report.CONNECT_TIMEOUT_S > 0
        assert report.STATEMENT_TIMEOUT_MS > 0

    def test_age_is_the_objection_age_not_the_session_lifespan(self):
        """Most sessions open and die inside a day, so lifespan printed 0d for
        objections standing for months."""
        import datetime as _dt
        row = _row(
            created_at=_dt.datetime(2026, 7, 1),
            updated_at=_dt.datetime(2026, 7, 1, 4),
            standing_since=_dt.datetime(2026, 7, 1, 2),
            now=_dt.datetime(2026, 9, 9),
        )
        out = report.render([row], max_conditions=4)
        # 69, not 0: the session lived 4 hours, the objection has stood 69 days.
        assert "69d" in out, out
        assert "[failed, 0d]" not in out

    def test_null_conditions_are_guarded(self):
        """21 synthesis rows have proposed_conditions NULL; an unguarded cast raises."""
        assert "jsonb_typeof" in report.QUERY


# ── acknowledgement ledger ──────────────────────────────────────────────────
#
# Every listed review is already status='failed' and nothing can close one, so
# the SessionStart surface re-listed all 46 forever (2026-09-24 triage: 31
# superseded, 8 stale, 5 test, 2 real). `ack` takes one off the default listing
# through a LOCAL append-only ledger. The properties pinned below: it never
# touches the database, it never guesses an id, a bad batch writes nothing,
# and a hidden review is always counted, never silently dropped.

import json
import types


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "acks.jsonl"
    monkeypatch.setenv(report.LEDGER_ENV, str(path))
    return path


def _ack_line(session_id, disposition="superseded", **over):
    row = dict(session_id=session_id, disposition=disposition, reason="PR merged",
               acknowledged_by="op", timestamp="2026-09-24T00:00:00+00:00",
               # bound to _row()'s default state; see ack_still_applies
               session_updated_at="2026-09-03T12:00:00+00:00", session_standing_since=None)
    row.update(over)
    return json.dumps(row)


class TestLedgerReadWrite:
    def test_the_ledger_path_is_overridable_by_env(self, ledger):
        assert report.ledger_path() == str(ledger)

    def test_the_default_ledger_lives_under_dot_unitares(self, monkeypatch):
        monkeypatch.delenv(report.LEDGER_ENV, raising=False)
        assert report.ledger_path().endswith("/.unitares/dialectic-acks.jsonl")

    def test_a_missing_ledger_hides_nothing(self, ledger):
        assert report.load_acks() == {}

    def test_malformed_lines_and_unknown_dispositions_hide_nothing(self, ledger):
        """A corrupt ledger must never make real backlog disappear."""
        ledger.parent.mkdir(parents=True)
        ledger.write_text("\n".join([
            "{not json",
            json.dumps(["a", "list"]),
            _ack_line("s-bad-disp", disposition="agrees"),
            json.dumps({"disposition": "stale"}),
            _ack_line("s-good"),
        ]) + "\n")
        assert set(report.load_acks()) == {"s-good"}

    def test_the_latest_row_for_a_session_wins(self, ledger):
        ledger.parent.mkdir(parents=True)
        ledger.write_text(_ack_line("s1", "test") + "\n" + _ack_line("s1", "stale") + "\n")
        assert report.load_acks()["s1"]["disposition"] == "stale"

    def test_append_creates_the_directory_and_never_truncates(self, ledger):
        report.append_acks([json.loads(_ack_line("s1"))])
        report.append_acks([json.loads(_ack_line("s2"))])
        lines = ledger.read_text().splitlines()
        assert [json.loads(ln)["session_id"] for ln in lines] == ["s1", "s2"]


class TestResolveIds:
    FULL_A = "a1b2c3d4e5f60718"
    FULL_B = "a1b2c3d4ffff0000"

    def test_a_unique_prefix_resolves_to_the_full_id(self):
        resolved, errors = report.resolve_ids(
            ["a1b2c3d4e5"], {"a1b2c3d4e5": [self.FULL_A]}, [self.FULL_A])
        assert (resolved, errors) == ([self.FULL_A], [])

    def test_an_ambiguous_prefix_is_refused_not_guessed(self):
        resolved, errors = report.resolve_ids(
            ["a1b2c3d4"], {"a1b2c3d4": [self.FULL_A, self.FULL_B]}, [self.FULL_A, self.FULL_B])
        assert resolved == []
        assert len(errors) == 1 and "ambiguous" in errors[0]
        assert self.FULL_A in errors[0] and self.FULL_B in errors[0]

    def test_ambiguity_counts_sessions_outside_the_backlog(self):
        """Unique among unresolved rows is not unique: the twin may be resolved."""
        resolved, errors = report.resolve_ids(
            ["a1b2c3d4"], {"a1b2c3d4": [self.FULL_A, self.FULL_B]}, [self.FULL_A])
        assert resolved == [] and "ambiguous" in errors[0]

    def test_an_unknown_id_is_refused(self):
        resolved, errors = report.resolve_ids(["deadbeef"], {"deadbeef": []}, [])
        assert resolved == [] and "no dialectic session" in errors[0]

    def test_a_session_not_in_the_backlog_is_refused(self):
        resolved, errors = report.resolve_ids(
            [self.FULL_A], {self.FULL_A: [self.FULL_A]}, [])
        assert resolved == [] and "not in the unresolved backlog" in errors[0]

    def test_a_too_short_prefix_is_refused_even_if_unique(self):
        resolved, errors = report.resolve_ids(["a1b2"], {"a1b2": [self.FULL_A]}, [self.FULL_A])
        assert resolved == [] and "shorter than" in errors[0]

    def test_two_prefixes_of_one_session_ack_it_once(self):
        resolved, errors = report.resolve_ids(
            ["a1b2c3d4e5", self.FULL_A],
            {"a1b2c3d4e5": [self.FULL_A], self.FULL_A: [self.FULL_A]}, [self.FULL_A])
        assert resolved == [self.FULL_A] and errors == []


def _mock_db(monkeypatch, sessions, backlog):
    """Mock the query layer: `sessions` is every id in the table, `backlog`
    the rows the unresolved query returns."""
    def fake_match(dsn, prefixes):
        return {p: [s for s in sessions if s.startswith(p)] for p in prefixes}

    monkeypatch.setattr(report, "fetch_matching_session_ids", fake_match)
    monkeypatch.setattr(report, "fetch", lambda dsn, window_days: [dict(r) for r in backlog])


class TestAckCommand:
    SESSIONS = ["1111aaaa22223333", "1111aaaa99990000", "4444bbbb55556666", "7777cccc88889999"]

    def _backlog(self):
        return [_row(session_id=s) for s in self.SESSIONS]

    def test_ack_appends_one_row_per_session_with_every_field(self, ledger, monkeypatch, capsys):
        _mock_db(monkeypatch, self.SESSIONS, self._backlog())
        rc = report.main(["ack", "4444bbbb", "7777cccc88889999",
                          "--disposition", "superseded", "--reason", "PR #2025 merged",
                          "--by", "kenny"])
        assert rc == 0
        rows = [json.loads(ln) for ln in ledger.read_text().splitlines()]
        assert [r["session_id"] for r in rows] == ["4444bbbb55556666", "7777cccc88889999"]
        for r in rows:
            assert set(r) == {"session_id", "disposition", "reason", "acknowledged_by", "timestamp",
                              "session_updated_at", "session_standing_since"}
            assert r["session_updated_at"] == "2026-09-03T12:00:00+00:00"
            assert r["disposition"] == "superseded"
            assert r["reason"] == "PR #2025 merged"
            assert r["acknowledged_by"] == "kenny"
            assert r["timestamp"]

    def test_one_bad_id_refuses_the_whole_batch(self, ledger, monkeypatch, capsys):
        """The operator is about to ack 44 at once; a half-applied batch is
        worse than a refused one."""
        _mock_db(monkeypatch, self.SESSIONS, self._backlog())
        rc = report.main(["ack", "4444bbbb", "1111aaaa",
                          "--disposition", "stale", "--reason", "old"])
        assert rc == 1
        assert not ledger.exists()
        assert "ambiguous" in capsys.readouterr().err

    def test_a_disposition_outside_the_set_is_rejected(self, ledger, monkeypatch):
        """In particular there is no way to record a verdict: `agrees` is not
        a disposition and an acknowledgement is not an approval."""
        _mock_db(monkeypatch, self.SESSIONS, self._backlog())
        with pytest.raises(SystemExit):
            report.main(["ack", "4444bbbb", "--disposition", "agrees", "--reason", "x"])
        assert not ledger.exists()

    def test_a_reason_is_required_and_must_not_be_blank(self, ledger, monkeypatch):
        _mock_db(monkeypatch, self.SESSIONS, self._backlog())
        with pytest.raises(SystemExit):
            report.main(["ack", "4444bbbb", "--disposition", "stale"])
        assert report.main(["ack", "4444bbbb", "--disposition", "stale", "--reason", "   "]) == 2
        assert not ledger.exists()

    def test_a_query_failure_writes_nothing(self, ledger, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(report, "fetch_matching_session_ids", boom)
        assert report.main(["ack", "4444bbbb", "--disposition", "stale", "--reason", "x"]) == 2
        assert not ledger.exists()


class TestListingHonoursAcks:
    def _setup(self, ledger, monkeypatch, acked):
        rows = [_row(session_id=s) for s in ("s-real", "s-merged", "s-smoke")]
        monkeypatch.setattr(report, "fetch", lambda dsn, window_days: [dict(r) for r in rows])
        if acked:
            ledger.parent.mkdir(parents=True)
            ledger.write_text("".join(_ack_line(s, d) + "\n" for s, d in acked))

    def test_default_text_hides_acked_and_counts_them_in_one_line(self, ledger, monkeypatch, capsys):
        self._setup(ledger, monkeypatch, [("s-merged", "superseded"), ("s-smoke", "test")])
        assert report.main([]) == 0
        out = capsys.readouterr().out
        assert "s-real" in out
        assert "s-merged" not in out and "s-smoke" not in out
        assert out.startswith("1 dialectic review(s)")
        summary = [ln for ln in out.splitlines() if "acknowledged review(s) hidden" in ln]
        assert len(summary) == 1
        assert summary[0].startswith("2 acknowledged") and "superseded 1" in summary[0] and "test 1" in summary[0]

    def test_all_acked_still_prints_the_hidden_count(self, ledger, monkeypatch, capsys):
        """Nothing vanishes silently: an empty listing still says what it hid."""
        self._setup(ledger, monkeypatch, [("s-real", "stale"), ("s-merged", "superseded"),
                                          ("s-smoke", "test")])
        report.main([])
        out = capsys.readouterr().out
        assert out.strip().startswith("3 acknowledged review(s) hidden")

    def test_no_acks_changes_nothing(self, ledger, monkeypatch, capsys):
        self._setup(ledger, monkeypatch, [])
        report.main([])
        out = capsys.readouterr().out
        assert out.startswith("3 dialectic review(s)") and "hidden" not in out

    def test_json_counts_only_visible_and_reports_what_it_hid(self, ledger, monkeypatch, capsys):
        """The SessionStart hook prints len(reviews); it must see the real 1."""
        self._setup(ledger, monkeypatch, [("s-merged", "superseded"), ("s-smoke", "test")])
        report.main(["--json"])
        d = json.loads(capsys.readouterr().out)
        assert [r["session_id"] for r in d["reviews"]] == ["s-real"]
        assert d["count"] == 1
        assert d["acknowledged_hidden"] == 2
        assert d["acknowledged_by_disposition"] == {"superseded": 1, "test": 1}

    def test_all_shows_everything_with_dispositions(self, ledger, monkeypatch, capsys):
        self._setup(ledger, monkeypatch, [("s-merged", "superseded")])
        report.main(["--all"])
        out = capsys.readouterr().out
        for s in ("s-real", "s-merged", "s-smoke"):
            assert s in out
        assert "ACKNOWLEDGED superseded by op" in out
        assert "hidden" not in out

    def test_all_json_carries_the_acknowledgement_per_row(self, ledger, monkeypatch, capsys):
        self._setup(ledger, monkeypatch, [("s-merged", "superseded")])
        report.main(["--all", "--json"])
        d = json.loads(capsys.readouterr().out)
        by_id = {r["session_id"]: r for r in d["reviews"]}
        assert d["count"] == 3
        assert by_id["s-merged"]["acknowledgement"]["disposition"] == "superseded"
        assert by_id["s-real"]["acknowledgement"] is None

    def test_an_acked_review_can_still_be_rendered_as_a_comment(self, ledger, monkeypatch, capsys):
        """Acknowledging hides a review from the list; it does not bury its conditions."""
        self._setup(ledger, monkeypatch, [("s-merged", "superseded")])
        assert report.main(["--session-id", "s-merged"]) == 0
        assert "s-merged" in capsys.readouterr().out


class TestNeverWritesTheDatabase:
    """⛔status is protocol state and `agrees` is the verdict; an acknowledgement
    is neither, so no path in this tool may write a dialectic row."""

    def _fake_psycopg2(self, monkeypatch, sessions):
        calls = {"readonly": [], "sql": []}

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                calls["sql"].append(sql)
                self._sql, self._params = sql, params

            def fetchall(self):
                if "starts_with" in self._sql:
                    return [{"prefix": p, "session_id": s}
                            for p in self._params["prefixes"] for s in sessions if s.startswith(p)]
                if "dialectic_messages" in self._sql:
                    return [_row(session_id=s) for s in sessions]
                return []

        class Conn:
            def set_session(self, readonly=False, **kw):
                calls["readonly"].append(readonly)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self, cursor_factory=None):
                return Cursor()

            def close(self):
                pass

        extras = types.ModuleType("psycopg2.extras")
        extras.RealDictCursor = object
        pg = types.ModuleType("psycopg2")
        pg.connect = lambda dsn, connect_timeout=None: Conn()
        pg.extras = extras
        monkeypatch.setitem(sys.modules, "psycopg2", pg)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
        return calls

    def test_ack_opens_every_connection_read_only_and_issues_no_writes(self, ledger, monkeypatch):
        calls = self._fake_psycopg2(monkeypatch, ["4444bbbb55556666"])
        assert report.main(["ack", "4444bbbb", "--disposition", "test", "--reason", "smoke"]) == 0
        assert calls["readonly"] and all(calls["readonly"])
        assert len(calls["readonly"]) == 2  # the id match and the backlog read
        import re
        write_verb = re.compile(r"\b(INSERT|UPDATE|DELETE|ALTER|TRUNCATE|MERGE)\b", re.I)
        for sql in calls["sql"]:
            assert not write_verb.search(sql), sql
        assert ledger.exists()

    def test_the_match_query_does_not_use_like_wildcards(self):
        """A typed `_` or `%` must not widen the match to an unnamed session."""
        assert "starts_with" in report.MATCH_QUERY
        assert "LIKE" not in report.MATCH_QUERY.upper()


class TestAckBoundToSessionState:
    """Review on #2428: reassign reopens the SAME session id, so an ack keyed
    only on the id would hide a new objection forever."""

    def test_an_ack_hides_the_session_it_was_taken_on(self):
        row = _row(session_id="s1")
        ack = json.loads(_ack_line("s1"))
        visible, hidden = report.partition([row], {"s1": ack})
        assert visible == [] and len(hidden) == 1

    def test_a_session_updated_after_the_ack_is_listed_again(self):
        row = _row(session_id="s1", updated_at=dt.datetime(2026, 9, 25, 9, 0))
        ack = json.loads(_ack_line("s1"))
        visible, hidden = report.partition([row], {"s1": ack})
        assert [r["session_id"] for r in visible] == ["s1"] and hidden == []

    def test_a_newer_objection_is_listed_again(self):
        row = _row(session_id="s1", standing_since=dt.datetime(2026, 9, 26))
        ack = json.loads(_ack_line("s1", session_standing_since="2026-09-02T00:00:00+00:00"))
        visible, _ = report.partition([row], {"s1": ack})
        assert [r["session_id"] for r in visible] == ["s1"]

    def test_an_ack_without_state_marks_hides_nothing(self):
        row = _row(session_id="s1")
        ack = json.loads(_ack_line("s1", session_updated_at=None))
        visible, _ = report.partition([row], {"s1": ack})
        assert [r["session_id"] for r in visible] == ["s1"]


class TestAckWriteAndInputEdges:
    def test_a_short_write_raises_instead_of_reporting_success(self, ledger, monkeypatch):
        monkeypatch.setattr(report.os, "write", lambda fd, data: 0)
        with pytest.raises(OSError):
            report.append_acks([{"session_id": "x"}])

    def test_partial_writes_are_completed(self, ledger, monkeypatch):
        real = report.os.write
        monkeypatch.setattr(report.os, "write", lambda fd, data: real(fd, data[:5]))
        report.append_acks([{"session_id": "abcdef0123456789", "disposition": "stale"}])
        assert json.loads(ledger.read_text())["session_id"] == "abcdef0123456789"

    def test_whitespace_only_ids_are_refused(self, ledger, monkeypatch, capsys):
        _mock_db(monkeypatch, [], [])
        rc = report.main(["ack", " ", "--disposition", "stale", "--reason", "x", "--by", "op"])
        assert rc == 2
        assert "no session ids" in capsys.readouterr().err
        assert not ledger.exists()


def test_all_json_reports_nothing_hidden_by_disposition(ledger, monkeypatch, capsys):
    """--all hides nothing, so the per-disposition hidden counts must be empty
    too, not a count of acknowledged rows that are in fact shown."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(_ack_line("abc123") + "\n")
    monkeypatch.setattr(report, "fetch", lambda dsn, window_days: [_row()])
    assert report.main(["--json", "--all"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["acknowledged_hidden"] == 0
    assert out["acknowledged_by_disposition"] == {}
    assert out["count"] == 1


class TestLedgerRobustness:
    """Review round 3 on #2428."""

    def test_an_undecodable_ledger_hides_nothing(self, ledger, capsys):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(b"\xff\xfe junk\n")
        assert report.load_acks() == {}
        assert "hiding nothing" in capsys.readouterr().err

    def test_a_torn_last_line_does_not_swallow_the_next_ack(self, ledger):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"session_id": "aaaa')
        report.append_acks([json.loads(_ack_line("b" * 16)), json.loads(_ack_line("c" * 16))])
        assert set(report.load_acks()) == {"b" * 16, "c" * 16}


class TestSeenAt:
    SID = "4444bbbb55556666"
    DB_NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)

    def test_a_session_changed_after_seen_at_is_refused(self, ledger, monkeypatch, capsys):
        _mock_db(monkeypatch, [self.SID],
                 [_row(session_id=self.SID, updated_at=dt.datetime(2026, 9, 25, 10, 0))])
        rc = report.main(["ack", self.SID, "--disposition", "stale", "--reason", "x",
                          "--by", "op", "--seen-at", "2026-09-24T12:00:00+00:00"])
        assert rc == 1
        assert "changed after --seen-at" in capsys.readouterr().err
        assert not ledger.exists()

    def test_an_unchanged_session_is_acknowledged(self, ledger, monkeypatch):
        _mock_db(monkeypatch, [self.SID], [_row(session_id=self.SID)])
        rc = report.main(["ack", self.SID, "--disposition", "stale", "--reason", "x",
                          "--by", "op", "--seen-at", "2026-09-24T12:00:00+00:00"])
        assert rc == 0 and ledger.exists()

    def test_a_seen_at_later_than_the_database_clock_is_refused(self, ledger, monkeypatch,
                                                                 capsys):
        """A future --seen-at would pass every change, the guard's whole purpose."""
        _mock_db(monkeypatch, [self.SID],
                 [_row(session_id=self.SID, updated_at=dt.datetime(2026, 9, 25, 10, 0),
                       now=self.DB_NOW)])
        rc = report.main(["ack", self.SID, "--disposition", "stale", "--reason", "x",
                          "--by", "op", "--seen-at", "2099-01-01T00:00:00+00:00"])
        assert rc == 1
        assert "later than the database's current time" in capsys.readouterr().err
        assert not ledger.exists()

    def test_a_seen_at_equal_to_the_database_clock_is_accepted(self, ledger, monkeypatch):
        _mock_db(monkeypatch, [self.SID], [_row(session_id=self.SID, now=self.DB_NOW)])
        rc = report.main(["ack", self.SID, "--disposition", "stale", "--reason", "x",
                          "--by", "op", "--seen-at", "2026-09-25T06:00:00-06:00"])
        assert rc == 0 and ledger.exists()

    def test_a_bad_seen_at_is_refused(self, ledger, monkeypatch):
        _mock_db(monkeypatch, [self.SID], [_row(session_id=self.SID)])
        rc = report.main(["ack", self.SID, "--disposition", "stale", "--reason", "x",
                          "--by", "op", "--seen-at", "yesterday"])
        assert rc == 2 and not ledger.exists()


def test_seen_at_without_an_offset_is_refused(ledger, monkeypatch, capsys):
    """Review round 5 on #2428: a naive time read as UTC let an unseen
    objection through for operators east of UTC."""
    sid = "4444bbbb55556666"
    _mock_db(monkeypatch, [sid], [_row(session_id=sid)])
    rc = report.main(["ack", sid, "--disposition", "stale", "--reason", "x",
                      "--by", "op", "--seen-at", "2026-09-24T10:00"])
    assert rc == 2
    assert "WITH an offset" in capsys.readouterr().err
    assert not ledger.exists()


# ── supersession ────────────────────────────────────────────────────────────
#
# The list could not see a LATER accepted review of the same subject, so the
# SessionStart count read 45-46 when the 2026-09-18 triage found 8 already
# answered by hand. Every one of those 8 is a fixture below, by the signal that
# actually matched it on the live database.


def _accepted(session_id, created, subject="", text=None, accepted=None):
    return dict(session_id=session_id, created_at=created, subject=subject,
                text=subject if text is None else text,
                accepted_at=accepted or created + dt.timedelta(minutes=30))


D = dt.datetime


class TestSubjectPr:
    @pytest.mark.parametrize("text, number, repo", [
        ("Code-review PR https://github.com/cirwel/unitares/pull/2025 at commit 530d36c3",
         2025, "cirwel/unitares"),
        ("Independent code review requested for cirwel/unitares PR #2348 (commit c8e0243e)",
         2348, "cirwel/unitares"),
        ("PR cirwel/unitares#2128 — Watcher detector reported a truncated prompt", 2128,
         "cirwel/unitares"),
        ("Design choice (plugin PR cirwel/unitares-governance-plugin#146, finding ...)",
         146, "cirwel/unitares-governance-plugin"),
        # The word before "PR" is never trusted as a repo: an adjective here...
        ("Review the current PR #2348 diff for correctness", 2348, None),
        # ...and a repo here, with nothing to tell the two apart.
        ("fermata PR #59: declared custody landed", 59, None),
        ("Independent final review of unitares PR #2025 at exact head 5dd21203", 2025, None),
        ("Review request for PR #2064 (draft, stacked on #2063)", 2064, None),
        ("PR #2316 caps the corroboration grade", 2316, None),
        ("Re-review the revised fix for issue #1817 / draft PR #1819", 1819, None),
    ])
    def test_the_first_pr_named_is_the_subject(self, text, number, repo):
        assert report.subject_pr(text) == report.PrRef(number, repo)

    @pytest.mark.parametrize("text", [
        "Scheduled canary probe (#1387 positive control).",
        "Is adoption measurable? The #1387 clock read shows ...",
        "Should AI systems have the right to refuse human commands?",
        "",
    ])
    def test_a_bare_number_or_no_pr_is_not_a_subject(self, text):
        assert report.subject_pr(text) is None

    def test_only_two_explicit_repos_can_disagree(self):
        plugin = report.PrRef(146, "cirwel/unitares-governance-plugin")
        assert not plugin.same_as(report.PrRef(146, "cirwel/unitares"))
        # A fork's #42 is not upstream's #42 (review round 1 on #2511).
        assert not report.PrRef(42, "alice/unitares").same_as(report.PrRef(42, "bob/unitares"))
        assert plugin.same_as(report.PrRef(146))
        assert not plugin.same_as(report.PrRef(147))


class TestFindSupersessions:
    def test_a_later_accepted_review_citing_the_session_supersedes_it(self):
        rows = [_row(session_id="196acc3739ea0008", created_at=D(2026, 8, 22, 1),
                     topic="Policy review: should a Phi cold-start spike ...")]
        later = [_accepted("a493adde00761526", D(2026, 8, 22, 9),
                           "Re-review the revised fix for draft PR #1819. Prior dialectic "
                           "196acc3739ea0008 correctly rejected ...")]
        assert report.find_supersessions(rows, later) == {
            "196acc3739ea0008": {"session_id": "a493adde00761526", "signal": "cites_session_id"}}

    def test_a_citation_in_the_thesis_text_counts(self):
        rows = [_row(session_id="2958a8ff5e266a6c", created_at=D(2026, 8, 30, 1), topic="x")]
        later = [_accepted("83edd2d6248f2050", D(2026, 8, 30, 9), "Exact-commit review",
                           text="Exact-commit review\nsupersedes 2958a8ff5e266a6c")]
        assert report.find_supersessions(rows, later)["2958a8ff5e266a6c"]["session_id"] == \
            "83edd2d6248f2050"

    def test_the_same_subject_pr_supersedes_every_earlier_rejection(self):
        rows = [
            _row(session_id="8da00e5b5d39e337", created_at=D(2026, 8, 29, 1),
                 topic="Independent code review of PR https://github.com/cirwel/unitares/pull/2025"),
            _row(session_id="0dc43b1d11e07bbb", created_at=D(2026, 8, 29, 2),
                 topic="Code-review PR https://github.com/cirwel/unitares/pull/2025 at 91cbe5be"),
        ]
        later = [_accepted("9cd71f4ad5933832", D(2026, 8, 29, 9),
                           "Independent final review of unitares PR #2025 at exact head 5dd21203")]
        found = report.find_supersessions(rows, later)
        assert set(found) == {"8da00e5b5d39e337", "0dc43b1d11e07bbb"}
        assert found["8da00e5b5d39e337"] == {
            "session_id": "9cd71f4ad5933832", "signal": "same_subject_pr", "pr": "cirwel/unitares#2025"}

    def test_an_earlier_acceptance_supersedes_nothing(self):
        rows = [_row(session_id="bbbb000011112222", created_at=D(2026, 9, 2),
                     topic="Review PR #2064")]
        earlier = [_accepted("aaaa000011112222", D(2026, 9, 1), "Review PR #2064",
                             text="Review PR #2064, and bbbb000011112222")]
        assert report.find_supersessions(rows, earlier) == {}

    def test_an_unqualified_pr_matches_a_url_for_the_same_number(self):
        """Live miss before the rule change: "the current PR #2348" read
        "current" as a repo, and 78129de5 stayed listed after faee73eb accepted
        the same PR twelve minutes later."""
        rows = [_row(session_id="78129de50c19837f", created_at=D(2026, 9, 23, 2, 52),
                     topic="Independent code review requested for cirwel/unitares PR #2348")]
        later = [_accepted("faee73eb2ef1db6f", D(2026, 9, 23, 3, 4),
                           "Review the current PR #2348 diff for correctness.")]
        assert report.find_supersessions(rows, later)["78129de50c19837f"]["session_id"] == \
            "faee73eb2ef1db6f"

    def test_a_pr_mentioned_in_passing_is_not_the_subject(self):
        """A review of #2064 "stacked on #2063" is not answered by accepting #2063."""
        rows = [_row(session_id="9f884c21e53c0026", created_at=D(2026, 9, 2),
                     topic="Review request for PR #2064 (draft, stacked on #2063)")]
        later = [_accepted("cccc000011112222", D(2026, 9, 3), "Review PR #2063")]
        assert report.find_supersessions(rows, later) == {}

    def test_a_shared_issue_number_is_not_a_shared_subject(self):
        """Live false positive, caught before shipping: a canary probe citing
        the #1387 clock hid a design review that had never been answered."""
        rows = [_row(session_id="c1db868e3323bad5", created_at=D(2026, 8, 16, 1),
                     topic="Is `request_review` adoption measurable? The #1387 clock read ...")]
        later = [_accepted("ecdd3c52affdc1f5", D(2026, 8, 16, 9),
                           "Scheduled canary probe: verifying the review surface (#1387 positive control).")]
        assert report.find_supersessions(rows, later) == {}

    def test_an_acceptance_older_than_the_objection_does_not_hide_it(self):
        """Review round 1 on #2511: A opened, B opened and accepted, then A's
        reviewer rejected. B's acceptance cannot have answered that."""
        rows = [_row(session_id="aaaa000011112222", created_at=D(2026, 9, 1, 1),
                     standing_since=D(2026, 9, 1, 5), topic="Review PR #3000")]
        later = [_accepted("bbbb000011112222", D(2026, 9, 1, 2), "Review PR #3000",
                           accepted=D(2026, 9, 1, 3))]
        assert report.find_supersessions(rows, later) == {}
        later[0]["accepted_at"] = D(2026, 9, 1, 6)
        assert set(report.find_supersessions(rows, later)) == {"aaaa000011112222"}

    def test_a_commit_sha_is_not_a_session_citation(self):
        rows = [_row(session_id="5db1710cf8bca186", created_at=D(2026, 8, 29), topic="x")]
        later = [_accepted("dddd000011112222", D(2026, 8, 30), "y",
                           text="at commit 5db1710cf8bca1861234567890abcdef12345678")]
        assert report.find_supersessions(rows, later) == {}


class TestSupersessionInTheListing:
    def _two(self, monkeypatch):
        rows = [
            _row(session_id="8da00e5b5d39e337", created_at=D(2026, 8, 29, 1),
                 topic="Code-review PR https://github.com/cirwel/unitares/pull/2025"),
            _row(session_id="open000011112222", created_at=D(2026, 8, 29, 2),
                 topic="Review PR #9999"),
        ]
        monkeypatch.setattr(report, "fetch", lambda dsn, window_days: [dict(r) for r in rows])
        monkeypatch.setattr(report, "fetch_superseders", lambda dsn, window_days: [
            _accepted("9cd71f4ad5933832", D(2026, 8, 29, 9), "unitares PR #2025 final review")])

    def test_superseded_reviews_leave_the_list_and_are_counted(self, ledger, monkeypatch, capsys):
        self._two(monkeypatch)
        assert report.main(["--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [r["session_id"] for r in out["reviews"]] == ["open000011112222"]
        assert out["count"] == 1
        assert out["superseded_hidden"] == 1

    def test_the_text_listing_says_how_many_it_hid(self, ledger, monkeypatch, capsys):
        self._two(monkeypatch)
        report.main([])
        out = capsys.readouterr().out
        assert "8da00e5b5d39e337" not in out
        assert "1 review(s) hidden as superseded" in out

    def test_all_shows_the_superseded_review_and_what_superseded_it(self, ledger, monkeypatch, capsys):
        self._two(monkeypatch)
        report.main(["--all"])
        out = capsys.readouterr().out
        assert "8da00e5b5d39e337" in out
        assert "SUPERSEDED by accepted review 9cd71f4ad5933832 (same subject PR cirwel/unitares#2025)" in out

    def test_all_keeps_the_acknowledgement_of_a_superseded_review(self, ledger, monkeypatch, capsys):
        """Review round 2 on #2511: the operator's recorded disposition must
        survive a later automatic supersession."""
        self._two(monkeypatch)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(_ack_line("8da00e5b5d39e337", reason="PR #2025 merged") + "\n")
        report.main(["--all"])
        out = capsys.readouterr().out
        assert "SUPERSEDED by accepted review 9cd71f4ad5933832" in out
        assert "ACKNOWLEDGED superseded" in out and "PR #2025 merged" in out

    def test_a_failed_supersession_read_hides_nothing(self, ledger, monkeypatch, capsys):
        self._two(monkeypatch)

        def boom(dsn, window_days):
            raise RuntimeError("db went away")

        monkeypatch.setattr(report, "fetch_superseders", boom)
        assert report.main(["--json"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["count"] == 2
        assert "hiding nothing" in captured.err


class TestSupersederQueryShape:
    def test_acceptance_is_the_reviewers_verdict_not_the_status(self):
        q = report.SUPERSEDER_QUERY
        assert "v.agrees IS TRUE" in q
        assert "dm.agent_id = s.reviewer_agent_id" in q
        assert "status" not in q

    def test_a_self_review_can_never_supersede(self):
        assert "s.reviewer_agent_id IS DISTINCT FROM s.paused_agent_id" in report.SUPERSEDER_QUERY

    def test_probes_cannot_supersede_organic_reviews(self):
        """Same frozen rule as QUERY (review round 1 on #2511)."""
        q = report.SUPERSEDER_QUERY
        assert "LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id" in q
        assert "(probe|canary)" in q and "^RP[0-9]" in q

    def test_the_acceptance_time_is_read(self):
        assert "v.timestamp AS accepted_at" in report.SUPERSEDER_QUERY
