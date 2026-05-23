from scripts.diagnostics import s22_candidate_envelope_coverage as coverage


def test_build_context_where_filters_by_comparison_key_and_since():
    since = coverage._parse_since("2026-05-08T00:00:00Z")
    where, params = coverage._build_context_where(
        json_expr="state_json->'provenance_context'",
        timestamp_column="recorded_at",
        comparison_key="r6-h1-2026-05-08",
        since=since,
    )

    assert where == (
        "WHERE state_json->'provenance_context' IS NOT NULL "
        "AND state_json->'provenance_context'->>'comparison_key' = $1 "
        "AND recorded_at >= $2::timestamptz"
    )
    assert params == ["r6-h1-2026-05-08", since]


def test_build_context_where_filters_by_since_only():
    since = coverage._parse_since("2026-05-08")
    where, params = coverage._build_context_where(
        json_expr="provenance->'s22_context'",
        timestamp_column="created_at",
        comparison_key=None,
        since=since,
    )

    assert where == (
        "WHERE provenance->'s22_context' IS NOT NULL "
        "AND created_at >= $1::timestamptz"
    )
    assert params == [since]


def test_parse_since_accepts_zulu_timestamp():
    assert coverage._parse_since("2026-05-08T00:00:00Z").isoformat() == (
        "2026-05-08T00:00:00+00:00"
    )
