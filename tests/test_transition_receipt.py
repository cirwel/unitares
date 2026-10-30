"""Deterministic transition-receipt contract and verifier tests."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.knowledge_authority import PROMOTION_SCHEMA
from src.knowledge_graph import DiscoveryNode
from src.transition_receipt import (
    TransitionReceiptError,
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
        "870411103c30deca369afe35cfd16a8d47ebabcd1f9f4da99293da4a1ee1d5a9"
    )
    assert receipt["receipt_id"] == (
        "tr_v1_02c1029098ca6245375b9f1c71e896426c0080b0fe25d944ef5f03c5e18a8225"
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
    valid, errors = verify_transition_receipt(receipt, stored)

    assert valid is True
    assert errors == []


def test_subject_projection_excludes_receipt_and_session_secret():
    subject = _promoted_discovery()
    before = knowledge_promotion_projection(subject)
    subject.provenance["transition_receipt"] = build_knowledge_promotion_receipt(subject)
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
