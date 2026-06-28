"""Regression tests for scripts/dev/adoption_kpi.py."""


def test_onboard_conversion_query_counts_beam_external_outcomes():
    from scripts.dev import adoption_kpi

    sql = adoption_kpi._snapshot_queries()["onboard_conversion"]

    assert "audit.outcome_events" in sql
    assert "oe.verification_source = 'external_signal'" in sql
    assert "oe.detail->>'harness' = 'beam'" in sql
    assert "ceremonial_checked_in OR beam_checked_in" in sql
    assert "ceremonial_converted" in sql
    assert "beam_converted" in sql
