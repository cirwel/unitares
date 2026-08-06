"""Adversarial acceptance tests for the isolated federation tracer."""

import pytest

pytest.importorskip("cryptography")

from scripts.demo.federation_tracer.tracer import run_trace


def test_two_governor_trace_exercises_expected_controls() -> None:
    trace = run_trace()

    assert trace["summary"] == {
        "all_expected_controls_pass": True,
        "case_count": 9,
        "passed_checks": 10,
        "total_checks": 10,
    }
    assert all(trace["checks"].values())
    assert trace["topology"]["shared_private_signing_key"] is False
    governors = trace["topology"]["governors"]
    assert governors[0]["pid"] != governors[1]["pid"]
    assert governors[0]["kid"] != governors[1]["kid"]


def test_authenticity_and_truth_are_reported_separately() -> None:
    result = run_trace()["cases"]["authentic_but_false_evidence"]

    assert result == {
        "accepted": False,
        "evidence_consistent": False,
        "origin_authentic": True,
        "reason": "evidence_mismatch",
    }


def test_stolen_authorization_token_alone_does_not_authorize() -> None:
    result = run_trace()["cases"]["stolen_token_without_holder_key"]

    assert result == {
        "accepted": False,
        "origin_authentic": True,
        "reason": "holder_key_mismatch",
    }
