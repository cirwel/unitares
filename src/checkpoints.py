"""Neutral checkpoint contracts, independent of assessment and transport.

Request bytes are transient, prepared by the caller before assessment mutates
its inputs. Only domain-separated HMACs and a scrubbed report excerpt are
retained. A receipt acknowledges capture, never permission or assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from typing import Any


CHECKPOINT_SCHEMA = "checkpoint.acceptance.v1"
CHECKPOINT_EVENT_SCHEMA = "checkpoint.recorded.v1"
EPISTEMIC_CLASSES = frozenset({
    "agent_report", "substrate_observation", "substrate_interpretation",
    "prediction", "synthetic",
})


class CheckpointConflict(ValueError):
    """One identity-scoped key was used for a different material request."""

    def __init__(self, canonical_checkpoint_id: str):
        super().__init__("checkpoint idempotency key already names a different request")
        self.canonical_checkpoint_id = canonical_checkpoint_id


@dataclass(frozen=True)
class CheckpointProvenance:
    """Allowlisted provenance; no caller-provided labels or proof strings."""

    epistemic_class: str = "agent_report"
    identity_tier: str = "unknown"
    proof_origin: str = "unknown"
    transport: str = "unknown"

    def __post_init__(self) -> None:
        allowed = {
            "epistemic_class": (EPISTEMIC_CLASSES, "agent_report"),
            "identity_tier": ({"strong", "medium", "weak", "unknown"}, "unknown"),
            "proof_origin": ({"caller_asserted", "server_inferred", "unknown"}, "unknown"),
            "transport": ({"stdio", "sse", "http", "mcp", "unknown"}, "unknown"),
        }
        for name, (values, fallback) in allowed.items():
            value = getattr(self, name)
            if not isinstance(value, str) or value not in values:
                object.__setattr__(self, name, fallback)

    def to_dict(self) -> dict[str, str]:
        return {
            "producer": "sync_state", "producer_version": "1",
            "epistemic_class": self.epistemic_class,
            "identity_tier": self.identity_tier,
            "proof_origin": self.proof_origin,
            "transport": self.transport,
        }


def checkpoint_request_bytes(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, bytes):
        return payload
    raise TypeError("checkpoint request_payload must be canonical bytes or text")


def checkpoint_digests(salt: bytes, key: str, payload: bytes | str) -> tuple[str, str]:
    """Salt is durable per stream; domains keep key and request hashes distinct."""
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError("checkpoint idempotency_key must contain 1 to 200 nonblank characters")
    if not isinstance(salt, bytes) or len(salt) != 32:
        raise ValueError("checkpoint digest salt must contain 32 bytes")
    key_digest = hmac.new(
        salt, b"unitares.checkpoint.key.v1\x00" + key.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    payload_digest = hmac.new(
        salt, b"unitares.checkpoint.request.v1\x00" + checkpoint_request_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return key_digest, payload_digest


@dataclass(frozen=True)
class CheckpointReceipt:
    checkpoint_id: str
    identity_id: int
    seq: int
    prev_checkpoint_id: str | None
    recorded_at: datetime
    accepted_attempt_id: str
    replayed: bool
    retry_safe: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CHECKPOINT_SCHEMA, "status": "accepted",
            "checkpoint_id": self.checkpoint_id, "event_id": self.checkpoint_id,
            "identity_id": self.identity_id, "seq": self.seq,
            "prev_checkpoint_id": self.prev_checkpoint_id,
            "recorded_at": self.recorded_at.astimezone(timezone.utc).isoformat(),
            "accepted_attempt_id": self.accepted_attempt_id,
            "idempotent_replay": self.replayed,
            "acceptance_retry_idempotent": self.retry_safe,
        }


@dataclass(frozen=True)
class CheckpointAssessmentClaim:
    """Execution receipt, not proof that every assessment effect was durable.

``started`` survives a lost acknowledgement or process death and is never
automatically reclaimed. ``completed`` means execution returned; it does not
upgrade the legacy assessment state's independent persistence receipt.
"""

    checkpoint_id: str
    assessor: str
    status: str
    claim_token: str | None
    claimed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "checkpoint.assessment_execution.v1",
            "checkpoint_id": self.checkpoint_id, "assessor": self.assessor,
            "status": self.status, "claimed": self.claimed,
            "automatic_retry": False,
        }
