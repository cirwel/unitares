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

    def test_canary_is_partitioned_on_label_not_trigger_source(self):
        assert "canary_dialectic" in report.QUERY
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

    def test_null_conditions_are_guarded(self):
        """21 synthesis rows have proposed_conditions NULL; an unguarded cast raises."""
        assert "jsonb_typeof" in report.QUERY
