"""Opt-in storage acknowledgements for handler tests that perform real check-ins."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.checkpoints import CheckpointAssessmentClaim, CheckpointReceipt
from src.db.base import IdentityRecord


@pytest.fixture
def durable_checkpoint_backend(_isolate_db_backend):
    """Model only identity write/read and unkeyed checkpoint delivery storage.

    Admission, the checkpoint callback, estimator execution and response shaping
    remain real. Unlike the default no-op DB, a successful identity write is
    readable and checkpoint operations acknowledge with the actual frozen types.
    Keyed replay/conflict and transaction semantics have dedicated storage tests.
    """
    db = _isolate_db_backend
    identities = {}
    streams = {}
    receipts = {}
    claims = {}

    async def upsert_identity(
        agent_id, api_key_hash, parent_agent_id=None, spawn_reason=None,
        metadata=None, created_at=None,
    ):
        now = datetime.now(timezone.utc)
        row = identities.get(agent_id)
        if row is None:
            row = IdentityRecord(
                identity_id=len(identities) + 1, agent_id=agent_id,
                api_key_hash=api_key_hash, created_at=created_at or now,
                updated_at=now, parent_agent_id=parent_agent_id,
                spawn_reason=spawn_reason, metadata=dict(metadata or {}),
            )
            identities[agent_id] = row
        else:
            row.updated_at = now
            row.metadata.update(metadata or {})
        return row.identity_id

    async def accept_checkpoint(
        *, agent_id, identity_id, idempotency_key, request_payload,
        report_excerpt, provenance, attempt_id,
    ):
        identity = identities[agent_id]
        assert identity.identity_id == identity_id and identity.status == "active"
        assert idempotency_key is None, "This handler fixture models only unkeyed calls"
        assert isinstance(request_payload, bytes)
        previous = streams.get(identity_id)
        receipt = CheckpointReceipt(
            checkpoint_id=str(uuid4()), identity_id=identity_id,
            seq=previous.seq + 1 if previous else 1,
            prev_checkpoint_id=previous.checkpoint_id if previous else None,
            recorded_at=datetime.now(timezone.utc), accepted_attempt_id=attempt_id,
            replayed=False, retry_safe=False,
        )
        streams[identity_id] = receipt
        receipts[receipt.checkpoint_id] = receipt
        return receipt

    async def claim_assessment(*, checkpoint_id, assessor):
        assert checkpoint_id in receipts and checkpoint_id not in claims
        claim = CheckpointAssessmentClaim(
            checkpoint_id=checkpoint_id, assessor=assessor, status="started",
            claim_token=str(uuid4()), claimed=True,
        )
        claims[checkpoint_id] = claim
        return claim

    async def finish_assessment(*, checkpoint_id, assessor, claim_token, status):
        claim = claims[checkpoint_id]
        assert (assessor, claim_token) == (claim.assessor, claim.claim_token)
        assert status in {"completed", "failed"}
        return True

    db.upsert_identity.side_effect = upsert_identity
    db.get_identity.side_effect = identities.get
    db.get_identity_by_id.side_effect = lambda identity_id: next(
        (row for row in identities.values() if row.identity_id == identity_id), None,
    )
    db.accept_checkpoint.side_effect = accept_checkpoint
    db.claim_checkpoint_assessment.side_effect = claim_assessment
    db.finish_checkpoint_assessment.side_effect = finish_assessment
    return db
