"""Tests for the frozen reviewer-verdict labeling evaluation."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from scripts.dev import dialectic_verdict_labels as labels


NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _records() -> list[dict]:
    base = {
        "created_at": NOW,
        "paused_agent_id": "paused",
        "reviewer_agent_id": "reviewer",
    }
    return [
        {
            **base,
            "session_id": "reject-session",
            "paused_label": "ordinary-agent",
            "message_id": 1,
            "message_agent_id": "reviewer",
            "message_type": "antithesis",
            "message_timestamp": NOW + timedelta(milliseconds=10),
            "agrees": None,
        },
        {
            **base,
            "session_id": "reject-session",
            "paused_label": "ordinary-agent",
            "message_id": 2,
            "message_agent_id": "reviewer",
            "message_type": "synthesis",
            "message_timestamp": NOW + timedelta(milliseconds=50),
            "agrees": False,
        },
        {
            **base,
            "session_id": "reject-session",
            "paused_label": "ordinary-agent",
            "message_id": 3,
            "message_agent_id": "paused",
            "message_type": "synthesis",
            "message_timestamp": NOW + timedelta(seconds=1),
            "agrees": True,
        },
        {
            **base,
            "session_id": "approve-session",
            "paused_label": "ordinary-agent-2",
            "message_id": 4,
            "message_agent_id": "reviewer",
            "message_type": "synthesis",
            "message_timestamp": NOW + timedelta(seconds=2),
            "agrees": True,
        },
        {
            **base,
            "session_id": "missing-session",
            "paused_label": "ordinary-agent-3",
            "message_id": None,
            "message_agent_id": None,
            "message_type": None,
            "message_timestamp": None,
            "agrees": None,
        },
        {
            **base,
            "session_id": "probe-session",
            "paused_label": "RateProbe1",
            "message_id": 5,
            "message_agent_id": "reviewer",
            "message_type": "synthesis",
            "message_timestamp": NOW + timedelta(seconds=3),
            "agrees": False,
        },
    ]


def _document(route_label: str = "needs_evidence") -> dict:
    return {
        "schema_version": 1,
        "cohort_id": labels.COHORT_ID,
        "annotations": [
            {
                "case_id": labels.case_id("reject-session"),
                "route_label": route_label,
                "confidence": "high",
                "basis_codes": ["bounded_evidence_gate"],
                "block_correctness": "unadjudicated",
            }
        ],
    }


def test_declared_probe_rule_is_frozen_not_retrospectively_expanded():
    assert labels.is_probe_family("AgreeRateProbe")
    assert labels.is_probe_family("canary_dialectic_123")
    assert labels.is_probe_family("RP18")
    assert not labels.is_probe_family("gates-dialectic-e2e")
    assert not labels.is_probe_family("UNITARES Test Agent")


def test_case_id_is_stable_scoped_and_does_not_expose_source_id():
    first = labels.case_id("sensitive-session-id")

    assert first == labels.case_id("sensitive-session-id")
    assert first != labels.case_id("another-session-id")
    assert first.startswith("rv-")
    assert "sensitive-session-id" not in first


def test_evaluate_keeps_population_and_review_denominators_separate():
    report = labels.evaluate(labels.build_sessions(_records()), _document())

    assert report["population"] == {
        "sessions": 4,
        "verdicts": 3,
        "rejected": 2,
        "approved": 1,
        "unknown": 0,
        "rejection_pct": 66.7,
    }
    assert report["review_cohort"] == {
        "sessions": 3,
        "verdicts": 2,
        "rejected": 1,
        "approved": 1,
        "unknown": 0,
        "missing_verdict": 1,
        "self_clear_after_reject": 1,
        "self_clear_after_reject_pct": 100.0,
        "reviewer_gap_median_ms": 40,
        "reviewer_gap_max_ms": 40,
    }
    assert report["routing"]["terminal_denial_mismatch_pct"] == 100.0
    assert report["routing"]["retryable_or_evidence_route_pct"] == 100.0
    assert report["routing"]["false_block_pct"] is None
    assert report["routing"]["false_block_status"].startswith("not_identified")


def test_complete_correctness_adjudication_identifies_false_block_rate():
    document = _document()
    document["annotations"][0]["block_correctness"] = "false_block"

    report = labels.evaluate(labels.build_sessions(_records()), document)

    assert report["routing"]["false_block_pct"] == 100.0
    assert report["routing"]["false_block_status"] == "identified"


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda item: item.update(route_label="approve"), "invalid route_label"),
        (lambda item: item.update(confidence="certain"), "invalid confidence"),
        (
            lambda item: item.update(block_correctness="probably_false"),
            "invalid block_correctness",
        ),
        (lambda item: item.update(reasoning="raw prose"), "privacy-unsafe"),
    ],
)
def test_annotation_validation_rejects_invalid_or_raw_fields(mutation, match):
    document = _document()
    mutation(document["annotations"][0])

    with pytest.raises(ValueError, match=match):
        labels.evaluate(labels.build_sessions(_records()), document)


def test_annotation_validation_requires_exact_rejection_coverage():
    document = _document()
    document["annotations"] = []

    with pytest.raises(ValueError, match="coverage mismatch"):
        labels.evaluate(labels.build_sessions(_records()), document)


def test_checked_in_annotations_pin_the_reviewed_route_distribution():
    document = json.loads(labels.DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    annotations = document["annotations"]

    assert len(annotations) == 18
    assert Counter(item["route_label"] for item in annotations) == {
        "deny": 2,
        "cooldown": 1,
        "needs_evidence": 11,
        "human": 4,
    }
    assert {item["block_correctness"] for item in annotations} == {
        "unadjudicated"
    }
    assert all(
        set(item)
        == {
            "case_id",
            "route_label",
            "confidence",
            "basis_codes",
            "block_correctness",
        }
        for item in annotations
    )


def test_verify_baseline_reports_field_level_differences():
    report = {
        "population": {
            "sessions": 54,
            "verdicts": 53,
            "rejected": 48,
            "approved": 5,
        },
        "review_cohort": {
            "sessions": 21,
            "verdicts": 20,
            "rejected": 18,
            "approved": 2,
            "missing_verdict": 1,
            "self_clear_after_reject": 13,
            "reviewer_gap_median_ms": 42,
            "reviewer_gap_max_ms": 64,
        },
    }

    with pytest.raises(ValueError, match="population_rejected"):
        labels.verify_baseline(report)
