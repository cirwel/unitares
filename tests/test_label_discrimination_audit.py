"""Tests for scripts/dev/label_discrimination_audit.py.

Offline: the classifier is a pure function, so the interesting behaviour is
testable without a database. The queries themselves are exercised by running
the script; what must not drift silently is the meaning of each verdict.
"""

import pytest


def _mod():
    from scripts.dev import label_discrimination_audit

    return label_discrimination_audit


def test_single_valued_label_is_degenerate():
    """The whole point: a field with one value cannot separate anything.

    `core.dialectic_sessions.trigger_source` was 'manual' on 44 of 44 rows.
    Nothing partitioning on it could ever return more than one bucket, and
    nothing warned the reader.
    """
    assert _mod()._verdict(rows=44, distinct=1, top_share=1.0) == "DEGENERATE"


def test_empty_window_is_not_a_finding():
    """No rows means no evidence, which is not the same as a degenerate label."""
    assert _mod()._verdict(rows=0, distinct=0, top_share=None) == "NO_DATA"


def test_near_constant_label_is_thin_not_ok():
    """99.1% one value still 'discriminates' by a naive distinct-count test.

    audit.r1_score_audit.verdict is 'inconclusive' on 10,282 of 10,371 rows.
    Any analysis resting on that field is resting on 89 rows, which is worth
    saying out loud rather than passing silently.
    """
    m = _mod()
    assert m._verdict(rows=10371, distinct=2, top_share=0.991) == "THIN"
    assert m._verdict(rows=10371, distinct=2, top_share=0.75) == "ok"


def test_thin_threshold_is_a_named_constant_not_a_literal():
    """So changing the bar is a visible edit, not a buried one."""
    m = _mod()
    assert 0.9 <= m.THIN_SHARE < 1.0
    assert m._verdict(rows=100, distinct=2, top_share=m.THIN_SHARE) == "THIN"


def test_payload_keys_are_judged_without_a_share():
    """jsonb keys report distinct-count only; a share would need a second scan.

    Passing top_share=None must NOT silently become THIN or crash.
    """
    m = _mod()
    assert m._verdict(rows=38591, distinct=823, top_share=None) == "ok"
    assert m._verdict(rows=46, distinct=1, top_share=None) == "DEGENERATE"


def test_audited_columns_carry_their_own_timestamp_column():
    """Each entry must name the ts column; audit tables do not agree on one.

    audit.outcome_events uses `ts`, audit.r1_score_audit uses `recorded_at`.
    Assuming a shared name raises UndefinedColumn at runtime, which is how
    this was found.
    """
    m = _mod()
    assert m.LABEL_COLUMNS, "no columns configured"
    for entry in m.LABEL_COLUMNS:
        table, column, ts_col = entry
        assert "." in table, f"{table} must be schema-qualified"
        assert column and ts_col
    by_table = {t: ts for t, _c, ts in m.LABEL_COLUMNS}
    assert by_table["audit.outcome_events"] == "ts"
    assert by_table["audit.r1_score_audit"] == "recorded_at"


@pytest.mark.parametrize("bad_days", [0, -1])
def test_non_positive_window_is_rejected(bad_days, monkeypatch, capsys):
    """A zero-day window would report every label as NO_DATA — a false all-clear."""
    import sys

    m = _mod()
    monkeypatch.setattr(sys, "argv", ["prog", "--days", str(bad_days)])
    with pytest.raises(SystemExit):
        m.main()
