"""Deterministic transition-receipt contract and verifier tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from src.knowledge_authority import PROMOTION_SCHEMA
from src.knowledge_graph import DiscoveryNode
from src.transition_receipt import (
    TransitionReceiptError,
    _receipt_id,
    build_knowledge_promotion_receipt,
    canonical_bytes,
    canonical_utc_timestamp,
    knowledge_promotion_projection,
    verify_transition_receipt,
)


def _promoted_discovery() -> DiscoveryNode:
    accepted_at = "2026-10-30T10:00:00.123456+00:00"
    return DiscoveryNode(
        id=accepted_at,
        agent_id="ad5df17e-cb56-4f9a-a702-890f9d6120c6",
        type="insight",
        summary="A bounded promoted claim",
        details="Repository configuration and an integration test agree.",
        tags=["authority-governed", "promoted-from-memory"],
        severity=None,
        timestamp=accepted_at,
        related_to=["memory-1", "evidence-1"],
        references_files=["config/example.json"],
        provenance={
            "source": "explicit_promotion",
            "writer_session_id_at_write": "secret-session-value",
            "knowledge_authority": {
                "schema": PROMOTION_SCHEMA,
                "source_id": "memory-1",
                "evidence_ids": ["evidence-1"],
                "promoted_by": "ad5df17e-cb56-4f9a-a702-890f9d6120c6",
                "promoted_at": accepted_at,
                "verification_basis": "repository and test evidence",
                "decision_standard": "one independent non-memory artifact",
            },
            "s22_context": {
                "schema": "s22.write_context.v1",
                "model": "gpt-example",
                "model_source": "caller_declared",
                "harness_type": "chatgpt",
                "identity_assurance": {"score": 1.0},
            },
        },
    )


def test_knowledge_promotion_receipt_golden_vector():
    receipt = build_knowledge_promotion_receipt(_promoted_discovery())

    assert receipt["subject_sha256"] == (
        "9fdad4fa996efd270081f96f3296303902f2f944b00b9f57a63a1a50b97951fa"
    )
    assert receipt["receipt_id"] == (
        "tr_v1_8c1a29827a9e2098975fb6e97e8e230830980bf06f4b90d3ce86f1e3fa3c3ecc"
    )
    assert receipt["accepted_at"] == "2026-10-30T10:00:00.123456Z"
    assert receipt["stance"] == "descriptive"
    assert receipt["authorizes"] == []
    assert receipt["runtime_provenance"] == {
        "model": "gpt-example",
        "model_source": "caller_declared",
        "harness_type": "chatgpt",
    }


def test_receipt_verifies_after_json_and_timestamp_round_trip():
    subject = _promoted_discovery()
    receipt = build_knowledge_promotion_receipt(subject)
    subject.provenance["transition_receipt"] = receipt

    stored = json.loads(json.dumps(subject.to_dict()))
    # PostgreSQL reads TIMESTAMPTZ back as an ISO string; this alternate UTC
    # rendering must still verify against the fixed receipt representation.
    stored["timestamp"] = "2026-10-30T10:00:00.123456Z"
    stored["provenance"]["knowledge_authority"]["promoted_at"] = (
        "2026-10-30T10:00:00.123456Z"
    )
    valid, errors = verify_transition_receipt(receipt, stored)

    assert valid is True
    assert errors == []


def test_projection_normalizes_datetime_promotion_timestamp():
    subject = _promoted_discovery().to_dict()
    accepted_at = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    subject["timestamp"] = accepted_at
    subject["provenance"]["knowledge_authority"]["promoted_at"] = accepted_at

    projection = knowledge_promotion_projection(subject)

    assert projection["timestamp"] == "2026-10-30T10:00:00.000000Z"
    assert projection["provenance"]["knowledge_authority"]["promoted_at"] == (
        "2026-10-30T10:00:00.000000Z"
    )


def test_subject_projection_excludes_receipt_and_session_secret():
    subject = _promoted_discovery()
    before = knowledge_promotion_projection(subject)
    subject.provenance["transition_receipt"] = build_knowledge_promotion_receipt(
        subject
    )
    after = knowledge_promotion_projection(subject)

    assert before == after
    assert "secret-session-value" not in canonical_bytes(after).decode()


def test_verifier_detects_subject_and_receipt_tampering():
    subject = _promoted_discovery()
    receipt = build_knowledge_promotion_receipt(subject)

    tampered_subject = deepcopy(subject.to_dict())
    tampered_subject["details"] = "different durable bytes"
    valid, errors = verify_transition_receipt(receipt, tampered_subject)
    assert valid is False
    assert "subject_hash_mismatch" in errors

    tampered_receipt = deepcopy(receipt)
    tampered_receipt["actor"]["agent_uuid"] = "another-agent"
    valid, errors = verify_transition_receipt(tampered_receipt, subject)
    assert valid is False
    assert "receipt_id_mismatch" in errors
    assert "actor_mismatch" in errors


def test_verifier_rejects_rehashed_verification_overclaims():
    subject = _promoted_discovery()
    receipt = build_knowledge_promotion_receipt(subject)
    receipt["verification"]["verified_fields"].append("operator_approval")
    receipt["verification"]["unverified_fields"].remove("operator_approval")
    receipt["verification"]["basis"] = "operator sign-off"
    receipt["receipt_id"] = _receipt_id(receipt)

    valid, errors = verify_transition_receipt(receipt, subject)

    assert valid is False
    assert errors == ["verification_contract_mismatch"]


def test_verifier_returns_error_for_non_string_verification_field():
    subject = _promoted_discovery()
    receipt = build_knowledge_promotion_receipt(subject)
    receipt["verification"]["verified_fields"] = [{}]
    receipt["receipt_id"] = _receipt_id(receipt)

    valid, errors = verify_transition_receipt(receipt, subject)

    assert valid is False
    assert errors == ["invalid_verification"]


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("verification_basis", None, "invalid_promotion_verification_basis"),
        ("verification_basis", "  ", "invalid_promotion_verification_basis"),
        ("decision_standard", None, "invalid_promotion_decision_standard"),
        ("decision_standard", "", "invalid_promotion_decision_standard"),
    ],
)
def test_projection_rejects_blank_promotion_policy_fields(field, value, error_code):
    subject = _promoted_discovery()
    subject.provenance["knowledge_authority"][field] = value

    with pytest.raises(TransitionReceiptError) as raised:
        knowledge_promotion_projection(subject)

    assert raised.value.code == error_code


def test_projection_rejects_blank_evidence_id_even_when_links_match():
    subject = _promoted_discovery()
    subject.provenance["knowledge_authority"]["evidence_ids"] = [""]
    subject.related_to = ["memory-1", ""]

    with pytest.raises(TransitionReceiptError) as raised:
        knowledge_promotion_projection(subject)

    assert raised.value.code == "invalid_promotion_evidence"


@pytest.mark.parametrize(
    "value, code",
    [
        ({"measurement": 0.5}, "float_not_canonical"),
        ({1: "non-string key"}, "non_string_key"),
        ({"too_large": 1 << 60}, "unsafe_integer"),
    ],
)
def test_canonical_bytes_rejects_ambiguous_values(value, code):
    with pytest.raises(TransitionReceiptError) as raised:
        canonical_bytes(value)
    assert raised.value.code == code


def test_canonical_timestamp_requires_an_explicit_offset():
    with pytest.raises(TransitionReceiptError) as raised:
        canonical_utc_timestamp("2026-10-30T10:00:00")
    assert raised.value.code == "naive_timestamp"
