from scripts.dev.wave3_prereq_data_window import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_WAIT,
    GateSummary,
    evaluate_gate,
)


def _summary(**overrides):
    values = {
        "measurement_type": "measurement.lease_plane.request",
        "row_count": 100,
        "first_row": "2026-06-10 13:46:34+00",
        "last_row": "2026-06-24 13:46:34+00",
        "elapsed_days": 14.1,
        "days_with_rows": 14,
        "last_row_age_hours": 0.5,
        "p50_ms": 4,
        "p95_ms": 10,
        "p99_ms": 40,
        "max_samples_dropped_total": 0,
        "invalid_samples_dropped_rows": 0,
    }
    values.update(overrides)
    return GateSummary(**values)


def test_gate_passes_when_window_is_mature_and_fresh():
    decision = evaluate_gate(
        _summary(),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "PASS"
    assert decision.exit_code == EXIT_PASS


def test_gate_waits_while_window_is_still_running():
    decision = evaluate_gate(
        _summary(elapsed_days=8.5, days_with_rows=9),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "WAIT"
    assert decision.exit_code == EXIT_WAIT
    assert any("window age" in reason for reason in decision.reasons)
    assert any("days with rows" in reason for reason in decision.reasons)


def test_gate_fails_when_channel_has_no_rows():
    decision = evaluate_gate(
        _summary(row_count=0, first_row=None, last_row=None),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "FAIL"
    assert decision.exit_code == EXIT_FAIL
    assert decision.reasons == ("no measurement rows found",)


def test_gate_fails_when_last_row_is_stale_even_if_window_is_old():
    decision = evaluate_gate(
        _summary(last_row_age_hours=49.0),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "FAIL"
    assert decision.exit_code == EXIT_FAIL
    assert any("stale" in reason for reason in decision.reasons)


def test_gate_fails_when_samples_were_dropped():
    decision = evaluate_gate(
        _summary(max_samples_dropped_total=1),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "FAIL"
    assert decision.exit_code == EXIT_FAIL
    assert any("samples dropped" in reason for reason in decision.reasons)


def test_gate_allows_explicit_samples_dropped_override():
    decision = evaluate_gate(
        _summary(max_samples_dropped_total=1),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=1,
    )

    assert decision.status == "PASS"
    assert decision.exit_code == EXIT_PASS


def test_gate_fails_when_samples_dropped_metadata_is_invalid():
    decision = evaluate_gate(
        _summary(invalid_samples_dropped_rows=2),
        min_days=14.0,
        min_days_with_rows=14,
        max_last_row_age_hours=24.0,
        max_samples_dropped_total=0,
    )

    assert decision.status == "FAIL"
    assert decision.exit_code == EXIT_FAIL
    assert any("invalid samples_dropped_total" in reason for reason in decision.reasons)
