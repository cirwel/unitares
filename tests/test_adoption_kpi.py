"""Regression tests for scripts/dev/adoption_kpi.py."""

from datetime import datetime, timezone

import pytest


def test_onboard_conversion_query_counts_beam_external_outcomes():
    from scripts.dev import adoption_kpi

    sql = adoption_kpi._snapshot_queries()["onboard_conversion"]

    assert "audit.outcome_events" in sql
    assert "oe.verification_source = 'external_signal'" in sql
    assert "oe.detail->>'harness' = 'beam'" in sql
    assert "ceremonial_checked_in OR beam_checked_in" in sql
    assert "ceremonial_converted" in sql
    assert "beam_converted" in sql


def test_review_nudge_conversion_is_same_session_action_and_bounded():
    from scripts.dev import adoption_kpi

    sql = adoption_kpi._snapshot_queries()["review_nudge_conversion"]

    assert "u.agent_id = n.agent_id" in sql
    assert "u.session_id = n.session_id" in sql
    assert "u.tool_name IN ('request_review', 'dialectic')" in sql
    assert "u.payload->>'action' = 'request'" in sql
    assert "u.success" in sql
    assert "make_interval(" in sql
    assert "%(nudge_conversion_minutes)s" in sql
    assert "%(nudge_until)s" in sql
    assert "core.dialectic_sessions" not in sql


def test_review_nudge_queries_use_exact_half_open_bounds():
    from scripts.dev import adoption_kpi

    queries = adoption_kpi._snapshot_queries()
    for key in ("review_nudge", "review_nudge_conversion"):
        sql = queries[key]
        assert "ts >= %(nudge_since)s" in sql
        assert "ts < %(nudge_until)s" in sql


def test_normalize_utc_bound_requires_timezone_and_normalizes():
    from scripts.dev import adoption_kpi

    parsed = adoption_kpi._normalize_utc_bound(
        "2026-08-17T01:30:00-06:00",
        name="nudge_since",
    )
    assert parsed == datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="explicit UTC offset"):
        adoption_kpi._normalize_utc_bound(
            "2026-08-17T01:30:00",
            name="nudge_since",
        )
