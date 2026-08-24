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


def test_kg_retrieval_counts_only_retrieval_actions():
    """The metric is named retrieval; housekeeping must not land in it.

    Before 2026-08-18 this query counted every `knowledge` action, so a
    resident's audit/cleanup/update sweep was reported as agent retrieval.
    """
    from scripts.dev import adoption_kpi

    sql = adoption_kpi._snapshot_queries()["agent_kg_retrieval"]

    assert "u.payload->>'action' IN ('search', 'details')" in sql
    # assert the tool LIST, not substring-absence — the query comment
    # explains why search_knowledge_graph was dropped, so it still appears
    assert "u.tool_name IN ('knowledge', 'search_shared_memory')" in sql
    # the broad count stays available so the checkpoint log's step-change
    # at the correction date is explainable rather than mysterious
    assert "all_action_calls" in sql
    assert "scheduled_searches" in sql


def test_surface_return_rate_excludes_hook_poll_and_scheduled_callers():
    """Continuation is only meaningful over calls outside the ceremony path.

    Named for the observation, not the motive: the repo forbids a metric name
    that presupposes agent volition, and the first version of this one
    ("elective") did exactly that.
    """
    from scripts.dev import adoption_kpi

    sql = adoption_kpi._snapshot_queries()["surface_return_rate"]

    # lifecycle ceremony and the polling surfaces are not the thing measured
    for excluded in (
        "process_agent_update",
        "get_governance_metrics",
        "list_agents",
        "onboard",
    ):
        assert excluded not in sql, f"{excluded} must not be counted"
    # dashboard reads of the dialectic are not participation
    assert "NOT IN ('get', 'list')" in sql
    # scheduled/harness-wired callers are classified only after they satisfy
    # the same eligibility predicate as denominator calls.
    assert "a.label ~* %(scheduled_re)s AS scheduled" in sql
    assert "FROM eligible_calls" in sql
    assert "WHERE NOT scheduled" in sql
    assert "%(return_gap_s)s" in sql


def test_scheduled_label_regex_is_shared_by_both_metrics():
    """One regex, so composition and continuation cannot drift apart.

    THIS TEST WAS PINNING A DEFECT. It asserted `"Vigil" in
    _SCHEDULED_LABEL_RE` — i.e. it required the resident roster to be
    hardcoded, which is what let the literal drift away from
    UNITARES_RESIDENTS and silently inflate the return rate (a resident not in
    the literal entered the denominator as an ordinary caller, and scheduled
    callers return by construction). The shared-regex property it was written
    to protect is real and is kept; the hardcoded-roster assertion is not.

    Residents now come from the roster and are checked against it, so the two
    still cannot drift — they cannot drift from each OTHER, and they cannot
    drift from the deployment either.
    """
    import re

    from src.grounding.class_indicator import load_resident_labels

    from scripts.dev import adoption_kpi

    queries = adoption_kpi._snapshot_queries()
    assert "%(scheduled_re)s" in queries["agent_kg_retrieval"]
    assert "%(scheduled_re)s" in queries["surface_return_rate"]

    pattern = adoption_kpi._scheduled_label_re()
    for job in ("Hermes Agent", "canary_", "kg-sweep"):
        assert re.match(pattern, job), job
    for resident in load_resident_labels():
        assert re.match(pattern, resident), resident
