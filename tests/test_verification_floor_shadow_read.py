"""The soak read must report a rate only when it has a denominator.

The instrument exists because a firing count alone could not answer the enable
decision's question (issue #2169). A reader that divides by whatever rows it
happens to have would reintroduce exactly that error in a different place, so
these tests pin the refusals as hard as the arithmetic.
"""

import json

import pytest

from scripts.analysis.verification_floor_shadow_read import (
    load_jsonl,
    render,
    summarize,
)
from src.verification_floor_shadow import EVENT_TYPE, SCHEMA


def _row(**overrides):
    row = {
        "schema": SCHEMA,
        "record_mode": "all",
        "measurement_scope": "live",
        "applied": False,
        "evaluated": True,
        "would_fire": False,
        "score": 0.0,
        "verdict": "safe",
        "categories": {},
        "category_count": 0,
        "matches": [],
        "abstained": False,
        "scoreable": True,
        "unscoreable_reason": None,
        "verdict_before": "safe",
        "verdict_after": "safe",
        "risk_before": 0.26,
        "risk_after": 0.26,
        "would_escalate_verdict": False,
        "risk_delta": 0.0,
        "text_chars": 120,
        "text_words": 20,
        "first_person": True,
        "live_action": "proceed",
        "live_sub_action": None,
        "live_verdict": "safe",
        "agent_id": "a1",
    }
    row.update(overrides)
    return row


def _firing(**overrides):
    return _row(
        would_fire=True,
        score=0.95,
        verdict="high-risk",
        categories={"audit_log_tampering": 0.55, "backup_destruction": 0.5},
        category_count=2,
        matches=["audit_log_tampering: disabled the audit logging"],
        verdict_after="high-risk",
        risk_after=0.95,
        would_escalate_verdict=True,
        risk_delta=0.69,
        **overrides,
    )


class TestRateNeedsADenominator:
    def test_rate_over_the_prose_denominator(self):
        report = summarize([_row() for _ in range(97)] + [_firing() for _ in range(3)])
        assert report["denominator"]["computable"] is True
        assert report["denominator"]["prose"] == 100
        assert report["firings"]["would_fire"] == 3
        assert report["rates"]["would_fire_over_prose"] == 0.03

    def test_firings_only_mode_reports_no_rate(self):
        # Three firings and nothing else is a numerator. Dividing it by itself
        # would report a 100% false-positive rate, which is the failure mode this
        # refusal exists to prevent.
        report = summarize([_firing(record_mode="firings") for _ in range(3)])
        assert report["rates"] == {}
        assert report["denominator"]["computable"] is False
        assert any("record_mode" in c for c in report["caveats"])

    def test_empty_window_names_the_four_states(self):
        report = summarize([])
        assert report["rates"] == {}
        caveat = " ".join(report["caveats"])
        for state in ("never_surfaced", "not_reachable", "not_recorded", "genuinely_quiet"):
            assert state in caveat

    def test_zero_firings_over_a_recorded_denominator_is_informative(self):
        report = summarize([_row() for _ in range(50)])
        assert report["rates"]["would_fire_over_prose"] == 0.0
        caveat = " ".join(report["caveats"])
        assert "the one zero here that IS informative" in caveat
        assert "establishes nothing about recall" in caveat


class TestPartitioning:
    def test_simulation_rows_are_excluded_from_rates(self):
        rows = [_row() for _ in range(10)] + [
            _firing(measurement_scope="simulation") for _ in range(10)
        ]
        report = summarize(rows)
        assert report["simulation_rows"] == 10
        assert report["live_rows"] == 10
        assert report["firings"]["would_fire"] == 0
        assert any("simulation row" in c for c in report["caveats"])

    def test_non_prose_callers_are_excluded_and_flagged(self):
        rows = [_row(first_person=False) for _ in range(90)] + [
            _firing() for _ in range(10)
        ]
        report = summarize(rows)
        assert report["denominator"]["scoreable"] == 100
        assert report["denominator"]["prose"] == 10
        # The wider denominator reads lower precisely because it includes
        # callers the detector could never have fired on.
        assert report["rates"]["would_fire_over_scoreable"] == 0.1
        assert report["rates"]["would_fire_over_prose"] == 1.0
        assert any("UNSCORED by this channel" in c for c in report["caveats"])

    def test_unscoreable_rows_are_named_not_counted_clean(self):
        rows = [
            _row(scoreable=False, unscoreable_reason="empty_response_text")
            for _ in range(5)
        ] + [_row() for _ in range(5)]
        report = summarize(rows)
        assert report["denominator"]["scoreable"] == 5
        assert report["denominator"]["unscoreable_reasons"] == {"empty_response_text": 5}

    def test_other_schema_versions_are_counted_but_never_pooled(self):
        report = summarize([_row(schema="verification_floor_shadow_v2")] + [_row()])
        assert report["rows_total"] == 2
        assert report["live_rows"] == 1


class TestFalsePositiveCandidates:
    def test_a_proceed_the_floor_would_have_raised_is_a_candidate(self):
        report = summarize([_row() for _ in range(9)] + [_firing(live_action="proceed")])
        assert report["firings"]["candidate_false_positives"] == 1
        assert report["rates"]["candidate_false_positive_over_prose"] == 0.1

    def test_an_escalation_the_fleet_also_paused_on_is_not_a_candidate(self):
        report = summarize(
            [_row() for _ in range(9)] + [_firing(live_action="pause")]
        )
        assert report["firings"]["candidate_false_positives"] == 0
        assert report["firings"]["escalations_the_fleet_also_acted_on"] == 1

    def test_categories_are_carried_for_adjudication(self):
        report = summarize([_row(), _firing()])
        assert report["firings"]["categories"]["audit_log_tampering"] == 1


class TestIO:
    def test_reads_raw_audit_log_lines(self, tmp_path):
        path = tmp_path / "audit_log.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-09-18T00:00:00",
                            "agent_id": "a1",
                            "event_type": EVENT_TYPE,
                            "confidence": 0.0,
                            "details": _firing(),
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-09-18T00:00:01",
                            "agent_id": "a1",
                            "event_type": "auto_attest",
                            "confidence": 0.0,
                            "details": {},
                        }
                    ),
                    "not json",
                ]
            )
        )
        rows = load_jsonl(str(path))
        assert len(rows) == 1
        report = summarize(rows)
        assert report["firings"]["would_fire"] == 1

    def test_render_is_readable_and_states_what_it_does_not_settle(self):
        text = render(summarize([_row(), _firing()]))
        assert "candidate FPs" in text
        assert "owners' sign-off" in text
