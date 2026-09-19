"""Unit cover for the 0.65 provenance-split diagnostic.

The script itself is read-only telemetry; what needs pinning is its
classification, because the whole point of the measurement is that
``server_set_only`` and ``caller_authored_only`` are not the same finding. A
bucket that silently merged them would produce a number that answers a
different question than the one asked — the exact failure mode CLAUDE.md's
measurement-authority section warns about.
"""

from datetime import datetime, timezone

import pytest

from scripts.diagnostics.outcome_evidence_provenance_split import (
    BUCKET_BOTH,
    BUCKET_CALLER_AUTHORED,
    BUCKET_NO_TRIGGER,
    BUCKET_SERVER_SET,
    _as_detail,
    _bucket_for,
    collect,
    render,
)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _query, *_params):
        return self._rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, rows):
        self._conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self._conn)


def _row(agent_id, detail, *, day=1):
    return {
        "ts": datetime(2026, 9, day, tzinfo=timezone.utc),
        "agent_id": agent_id,
        "detail": detail,
    }


def test_bucket_separates_server_set_from_caller_authored():
    assert _bucket_for({"phase5_emitter"}) == BUCKET_SERVER_SET
    assert _bucket_for({"kind_with_exit_code"}) == BUCKET_CALLER_AUTHORED
    assert _bucket_for({"phase5_emitter", "kind_with_exit_code"}) == BUCKET_BOTH
    assert _bucket_for(set()) == BUCKET_NO_TRIGGER


def test_detail_is_read_whether_stored_as_jsonb_or_text():
    assert _as_detail({"a": 1}) == {"a": 1}
    assert _as_detail('{"a": 1}') == {"a": 1}
    assert _as_detail("not json") == {}
    assert _as_detail(None) == {}


@pytest.mark.asyncio
async def test_ungraded_rows_are_counted_apart_from_weak_grades():
    """A pre-grader row is 'not recorded', not 'graded and found weak'. Folding
    the two together is what turns a coverage gap into a false zero."""
    pool = _FakePool([
        _row("a1", {"summary": "no grade key at all"}),
        _row("a2", {"corroboration_grade": "claim_only"}),
    ])

    snapshot = await collect(pool, None)

    assert snapshot["rows_total"] == 2
    assert snapshot["rows_ungraded"] == 1
    assert snapshot["grade_histogram"]["claim_only"] == 1
    assert sum(snapshot["grade_histogram"].values()) == 1


@pytest.mark.asyncio
async def test_admitted_count_tracks_the_actuating_cut_not_the_grade():
    """Sitting at 0.65 is not the same as training calibration: a row still has
    to be a hard exogenous signal and not excluded. Reporting the tier alone
    would overstate what reaches the tactical channel."""
    pool = _FakePool([
        _row("a1", {
            "corroboration_grade": "tool_observed",
            "kind": "test",
            "exit_code": 0,
            "hard_exogenous_signal": "test_result",
        }),
        _row("a2", {
            "corroboration_grade": "tool_observed",
            "kind": "test",
            "exit_code": 0,
            "hard_exogenous_signal": "test_result",
            "calibration_excluded": True,
            "calibration_exclusion_reasons": ["shadow_write"],
        }),
        _row("a3", {
            "corroboration_grade": "tool_observed",
            "kind": "test",
            "exit_code": 0,
        }),
    ])

    snapshot = await collect(pool, None)
    caller = snapshot["tool_observed_buckets"][BUCKET_CALLER_AUTHORED]

    assert caller["rows"] == 3
    assert caller["calibration_admitted"] == 1
    assert caller["distinct_agents"] == 3
    assert snapshot["calibration_exclusion_census"] == {"shadow_write": 1}


@pytest.mark.asyncio
async def test_server_set_and_clamped_rows_land_in_their_own_buckets():
    pool = _FakePool([
        _row("srv", {"corroboration_grade": "tool_observed", "phase5_emitter": True}),
        _row("mix", {
            "corroboration_grade": "tool_observed",
            "phase5_emitter": True,
            "tool_results": [{"ok": True}],
        }),
        _row("cap", {
            "corroboration_grade": "tool_observed",
            "source": "server_observation",
            "verified": True,
        }),
    ])

    snapshot = await collect(pool, None)
    buckets = snapshot["tool_observed_buckets"]

    assert buckets[BUCKET_SERVER_SET]["rows"] == 1
    assert buckets[BUCKET_BOTH]["rows"] == 1
    assert buckets[BUCKET_NO_TRIGGER]["rows"] == 1
    assert buckets[BUCKET_CALLER_AUTHORED]["rows"] == 0
    assert snapshot["trigger_census"]["phase5_emitter"] == 2


@pytest.mark.asyncio
async def test_report_states_what_the_numbers_do_not_establish():
    """The caveat is part of the output, not of the commit message. A number
    that travels without it is the thing the measurement rule forbids."""
    snapshot = await collect(_FakePool([]), None)
    text = render(snapshot)

    assert "telemetry" in text.lower()
    assert "authorizes no removal" in text
    assert "NOT a weak grade" in text
