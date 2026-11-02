"""Neutral contract privacy, canonical digest, and response semantics."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.checkpoints import (
    CheckpointAssessmentClaim, CheckpointProvenance, CheckpointReceipt,
    checkpoint_digests,
)


def test_digest_is_stable_domain_separated_and_identity_salted():
    salt = b"a" * 32
    key, payload = "logical request", b'{"schema":1,"report":"full transient report"}'
    assert checkpoint_digests(salt, key, payload) == checkpoint_digests(salt, key, payload.decode())
    first = checkpoint_digests(salt, key, payload)
    assert first != checkpoint_digests(b"b" * 32, key, payload)
    assert first[0] != checkpoint_digests(salt, key + "x", payload)[0]
    assert first[1] != checkpoint_digests(salt, key, payload + b"x")[1]
    key_digest, request_digest = checkpoint_digests(salt, "same", b"same")
    assert key_digest != request_digest


@pytest.mark.parametrize("key", ["", "   ", "a" * 201, 123, None])
def test_invalid_caller_keys_are_rejected(key):
    with pytest.raises(ValueError):
        checkpoint_digests(b"a" * 32, key, b"request")


def test_provenance_never_persists_arbitrary_labels_or_proof_strings():
    provenance = CheckpointProvenance(
        epistemic_class="substrate_interpretation", identity_tier="private arbitrary text",
        proof_origin="secret-proof", transport="unknown private hostname",
    )
    assert provenance.to_dict() == {
        "producer": "sync_state", "producer_version": "1",
        "epistemic_class": "substrate_interpretation", "identity_tier": "unknown",
        "proof_origin": "unknown", "transport": "unknown",
    }
    with pytest.raises(FrozenInstanceError):
        provenance.transport = "http"


def test_receipt_is_immutable_and_utc_and_acknowledges_acceptance_only():
    receipt = CheckpointReceipt(
        "checkpoint", 1, 2, "previous",
        datetime(2026, 11, 2, 12, tzinfo=timezone(timedelta(hours=-7))),
        "attempt", True, True,
    )
    data = receipt.to_dict()
    assert data["recorded_at"] == "2026-11-02T19:00:00+00:00"
    assert data["event_id"] == data["checkpoint_id"]
    assert data["acceptance_retry_idempotent"] is True
    assert "checkin_retry_idempotent" not in data
    assert not any(field in data for field in ("permission", "E", "I", "S", "V", "verdict"))
    with pytest.raises(FrozenInstanceError):
        receipt.seq = 3


def test_execution_claim_projection_never_exposes_claim_token_or_promises_redelivery():
    claim = CheckpointAssessmentClaim("checkpoint", "test-assessor", "started", "secret-token", False)
    projected = claim.to_dict()
    assert projected["automatic_retry"] is False
    assert "secret-token" not in json.dumps(projected)
