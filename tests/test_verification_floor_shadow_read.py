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
        "fired": False,
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
        "escalated_verdict": False,
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
        fired=True,
        score=0.95,
        verdict="high-risk",
        categories={"audit_log_tampering": 0.55, "backup_destruction": 0.5},
        category_count=2,
        matches=["audit_log_tampering: disabled the audit logging"],
        verdict_after="high-risk",
        risk_after=0.95,
        escalated_verdict=True,
        risk_delta=0.69,
        **overrides,
    )


class TestRateNeedsADenominator:
    def test_rate_over_the_prose_denominator(self):
        report = summarize([_row() for _ in range(97)] + [_firing() for _ in range(3)])
        assert report["denominator"]["computable"] is True
        assert report["strata"]["first_person"]["rows"] == 100
        assert report["firings"]["fired"] == 3
        assert report["rates"]["fired"] == 0.03

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
        assert report["rates"]["fired"] == 0.0
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
        assert report["firings"]["fired"] == 0
        assert any("simulation row" in c for c in report["caveats"])

    def test_no_rate_can_exceed_one(self):
        """Regression: numerator and denominator must come from one population.

        The first draft took firings from every live row and divided by
        first-person rows only, on the false premise that the detector needs a
        pronoun to fire. Two clean first-person rows plus six pronoun-free
        escalating firings reported a 300% false-positive rate — on the exact
        number the enable gate consumes.
        """
        rows = [_row() for _ in range(2)] + [
            _firing(first_person=False) for _ in range(6)
        ]
        report = summarize(rows)
        for name, value in report["rates"].items():
            assert value is not None and 0.0 <= value <= 1.0, f"{name}={value}"
        for stratum in report["strata"].values():
            for name, value in stratum["rates"].items():
                assert value is None or 0.0 <= value <= 1.0, f"{name}={value}"

    def test_pronoun_free_firings_are_a_stratum_not_a_dropped_row(self):
        rows = [_row() for _ in range(2)] + [
            _firing(first_person=False) for _ in range(6)
        ]
        report = summarize(rows)
        # Counted in the headline, because they are real firings on real traffic.
        assert report["firings"]["fired"] == 6
        assert report["rates"]["fired"] == 0.75
        # And attributed to their own stratum, each rate inside it.
        assert report["strata"]["pronoun_free"]["rows"] == 6
        assert report["strata"]["pronoun_free"]["fired"] == 6
        assert report["strata"]["pronoun_free"]["rates"]["fired"] == 1.0
        assert report["strata"]["first_person"]["rows"] == 2
        assert report["strata"]["first_person"]["fired"] == 0
        assert any("does not require one" in c for c in report["caveats"])

    def test_applied_rows_are_excluded_from_the_pending_decision_rate(self):
        # An enforced escalation is not evidence about a decision not yet taken.
        rows = [_row() for _ in range(10)] + [
            _firing(applied=True) for _ in range(10)
        ]
        report = summarize(rows)
        assert report["applied_rows"] == 10
        assert report["shadow_rows"] == 10
        assert report["firings"]["fired"] == 0
        assert any("applied=true" in c for c in report["caveats"])

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
        assert report["rates"]["escalated_while_fleet_proceeded"] == 0.1

    def test_an_escalation_the_fleet_also_paused_on_is_not_a_candidate(self):
        report = summarize(
            [_row() for _ in range(9)] + [_firing(live_action="pause")]
        )
        assert report["firings"]["candidate_false_positives"] == 0
        assert report["strata"]["first_person"]["escalated_verdict"] == 1

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
        assert report["firings"]["fired"] == 1

    def test_render_is_readable_and_states_what_it_does_not_settle(self):
        text = render(summarize([_row(), _firing()]))
        assert "candidate FPs" in text
        assert "owners' sign-off" in text
