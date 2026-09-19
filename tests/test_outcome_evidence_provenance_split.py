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


def _row(agent_id, detail, *, day=1, outcome_type="task_completed"):
    return {
        "ts": datetime(2026, 9, day, tzinfo=timezone.utc),
        "agent_id": agent_id,
        "outcome_type": outcome_type,
        "detail": detail,
    }


def test_the_no_trigger_bucket_is_named_for_a_state_not_a_cause():
    """Only the FINAL grade is persisted, so a tool_observed row with no
    recomputed trigger could have been clamped down OR could have carried a
    trigger the current vocabulary no longer recognises. The bucket name must
    not pick one; an external review caught the earlier `no_trigger_clamped`
    asserting a history the data cannot establish."""
    assert BUCKET_NO_TRIGGER == "no_trigger_recomputed"
    assert "clamped" not in BUCKET_NO_TRIGGER


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
async def test_eligibility_tracks_the_real_gate_not_a_proxy_field():
    """Sitting at 0.65 is not the same as training calibration, and
    `hard_exogenous_signal` is not the gate.

    The gate in `_record_outcome_event_inline` is: created, a reported
    confidence, weight over the threshold, and not excluded. Only THEN does
    `outcome_type in HARD_EXOGENOUS_TYPES` select the narrower tactical lane.
    An earlier version of this script keyed eligibility on the
    `hard_exogenous_signal` field and undercounted the general channel; an
    external review (gpt-5.6-terra, 2026-09-19) caught it.
    """
    pool = _FakePool([
        # Eligible, and in the tactical lane.
        _row("a1", {"corroboration_grade": "tool_observed", "kind": "test",
                    "exit_code": 0, "reported_confidence": 0.8},
             outcome_type="test_passed"),
        # Excluded -> not eligible at all, despite a confidence.
        _row("a2", {"corroboration_grade": "tool_observed", "kind": "test",
                    "exit_code": 0, "reported_confidence": 0.8,
                    "calibration_excluded": True,
                    "calibration_exclusion_reasons": ["shadow_write"]},
             outcome_type="test_passed"),
        # No reported confidence -> nothing to calibrate against.
        _row("a3", {"corroboration_grade": "tool_observed", "kind": "test",
                    "exit_code": 0},
             outcome_type="test_passed"),
        # Eligible for the GENERAL channel but not the tactical lane: this is
        # the row the old proxy dropped.
        _row("a4", {"corroboration_grade": "tool_observed", "kind": "command",
                    "exit_code": 0, "reported_confidence": 0.6},
             outcome_type="drawing_completed"),
    ])

    snapshot = await collect(pool, None)
    caller = snapshot["tool_observed_buckets"][BUCKET_CALLER_AUTHORED]

    assert caller["rows"] == 4
    assert caller["calibration_eligible"] == 2
    assert caller["tactical_channel"] == 1
    assert caller["distinct_agents"] == 4
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
