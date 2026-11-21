"""Transactional neutral checkpoint append and isolated assessment claims.

Callers must authenticate and scope the identity before invoking this storage
API. Existing-record lookup is a read; new append additionally locks and checks
durable lifecycle state through the identity owner on this same connection.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import uuid
from dataclasses import replace

from src.checkpoints import (
    CheckpointAssessmentClaim, CheckpointConflict, CheckpointProvenance,
    CheckpointReceipt, checkpoint_digests, checkpoint_request_bytes,
)
from src.report_excerpt import make_report_excerpt


def checkpoint_receipt(row, *, replayed: bool, retry_safe: bool) -> CheckpointReceipt:
    return CheckpointReceipt(
        checkpoint_id=str(row["checkpoint_id"]), identity_id=row["identity_id"],
        seq=row["seq"],
        prev_checkpoint_id=str(row["prev_checkpoint_id"]) if row["prev_checkpoint_id"] else None,
        recorded_at=row["recorded_at"], accepted_attempt_id=str(row["accepted_attempt_id"]),
        replayed=replayed, retry_safe=retry_safe,
    )


def checked_checkpoint_key(key: str | None) -> str:
    if key is None:
        return str(uuid.uuid4())
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError("checkpoint idempotency_key must contain 1 to 200 nonblank characters")
    return key


def checked_assessor(assessor: str) -> str:
    if not isinstance(assessor, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,79}", assessor):
        raise ValueError("invalid checkpoint assessor identifier")
    return assessor


class CheckpointMixin:
    async def lookup_checkpoint(
        self, *, agent_id: str, identity_id: int, idempotency_key: str, request_payload: bytes | str,
    ) -> CheckpointReceipt | None:
        """Recover an authenticated identity's receipt without new acceptance."""
        if idempotency_key is None:
            raise ValueError("checkpoint lookup requires an explicit idempotency key")
        key = checked_checkpoint_key(idempotency_key)
        payload = checkpoint_request_bytes(request_payload)
        async with self.acquire() as conn:
            await self.verify_checkpoint_identity_scope(conn, agent_id, identity_id)
            stream = await conn.fetchrow(
                "SELECT digest_salt FROM core.checkpoint_streams WHERE identity_id = $1",
                identity_id,
            )
            if stream is None:
                return None
            key_digest, request_digest = checkpoint_digests(stream["digest_salt"], key, payload)
            row = await conn.fetchrow(
                "SELECT * FROM core.checkpoints WHERE identity_id = $1 AND idempotency_key_digest = $2",
                identity_id, key_digest,
            )
            if row is None:
                return None
            if not hmac.compare_digest(row["request_digest"], request_digest):
                raise CheckpointConflict(str(row["checkpoint_id"]))
            return checkpoint_receipt(row, replayed=True, retry_safe=True)

    async def accept_checkpoint(
        self, *, agent_id: str, identity_id: int, idempotency_key: str | None,
        request_payload: bytes | str, report_excerpt: str | None,
        provenance: CheckpointProvenance, attempt_id: str,
        record_activity: bool = False,
    ) -> CheckpointReceipt:
        """Commit one neutral occurrence, its order, and its event atomically.

        ``record_activity`` additionally commits neutral admission history in
        this transaction. It is internal opt-in for check-ins without an
        assessor; the existing assessed path retains its own accounting.

        Exceptions propagate: a connection failure/cancellation can follow a
        committed transaction. Retry with the original explicit key and bytes;
        never interpret an exception as proof that no checkpoint was accepted.
        """
        if type(identity_id) is not int or identity_id < 1:
            raise ValueError("checkpoint identity_id must be positive")
        if not isinstance(provenance, CheckpointProvenance):
            raise TypeError("checkpoint provenance must be allowlisted")
        key = checked_checkpoint_key(idempotency_key)
        payload = checkpoint_request_bytes(request_payload)
        attempt_uuid = uuid.UUID(attempt_id)
        excerpt = make_report_excerpt(report_excerpt)
        retry_safe = idempotency_key is not None
        async with self.acquire() as conn:
            async with conn.transaction():
                await self.verify_checkpoint_identity_scope(conn, agent_id, identity_id)
                # Immutable receipt recovery needs no new lifecycle permission.
                # For a new append acquire the lifecycle locks BEFORE any FK
                # INSERT takes a key-share lock on identities. Reversing these
                # locks can deadlock against pause's agents -> identities order.
                prior_stream = await conn.fetchrow(
                    "SELECT digest_salt FROM core.checkpoint_streams WHERE identity_id = $1",
                    identity_id,
                )
                if prior_stream is not None:
                    prior_key, prior_digest = checkpoint_digests(prior_stream["digest_salt"], key, payload)
                    prior = await conn.fetchrow(
                        "SELECT * FROM core.checkpoints WHERE identity_id = $1 AND idempotency_key_digest = $2",
                        identity_id, prior_key,
                    )
                    if prior is not None:
                        if not hmac.compare_digest(prior["request_digest"], prior_digest):
                            raise CheckpointConflict(str(prior["checkpoint_id"]))
                        return checkpoint_receipt(prior, replayed=True, retry_safe=retry_safe)
                locked_identity = await self.lock_active_checkpoint_identity(conn, agent_id)
                if locked_identity != identity_id:
                    raise ValueError("checkpoint identity scope does not match authenticated agent")
                await conn.execute(
                    "INSERT INTO core.checkpoint_streams (identity_id, digest_salt) VALUES ($1, $2) "
                    "ON CONFLICT (identity_id) DO NOTHING", identity_id, secrets.token_bytes(32),
                )
                stream = await conn.fetchrow(
                    "SELECT * FROM core.checkpoint_streams WHERE identity_id = $1 FOR UPDATE",
                    identity_id,
                )
                key_digest, request_digest = checkpoint_digests(stream["digest_salt"], key, payload)
                existing = await conn.fetchrow(
                    "SELECT * FROM core.checkpoints WHERE identity_id = $1 AND idempotency_key_digest = $2",
                    identity_id, key_digest,
                )
                if existing is not None:
                    if not hmac.compare_digest(existing["request_digest"], request_digest):
                        raise CheckpointConflict(str(existing["checkpoint_id"]))
                    result = checkpoint_receipt(existing, replayed=True, retry_safe=retry_safe)
                else:
                    row = await conn.fetchrow(
                        "INSERT INTO core.checkpoints "
                        "(checkpoint_id, identity_id, seq, prev_checkpoint_id, idempotency_key_digest, "
                        "request_digest, accepted_attempt_id, report_excerpt, provenance) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) RETURNING *",
                        uuid.uuid4(), identity_id, stream["last_seq"] + 1, stream["last_checkpoint_id"],
                        key_digest, request_digest, attempt_uuid, excerpt, json.dumps(provenance.to_dict()),
                    )
                    await conn.execute(
                        "INSERT INTO core.checkpoint_outbox (event_id, identity_id, seq, recorded_at) "
                        "VALUES ($1, $2, $3, $4)", row["checkpoint_id"], identity_id,
                        row["seq"], row["recorded_at"],
                    )
                    await conn.execute(
                        "UPDATE core.checkpoint_streams SET last_seq = $2, last_checkpoint_id = $3 "
                        "WHERE identity_id = $1", identity_id, row["seq"], row["checkpoint_id"],
                    )
                    result = checkpoint_receipt(row, replayed=False, retry_safe=retry_safe)
                    if record_activity:
                        activity = await self.record_checkpoint_activity(
                            conn, agent_id, row["recorded_at"],
                        )
                        result = replace(result, activity=activity)
            return result

    async def claim_checkpoint_assessment(
        self, *, checkpoint_id: str, assessor: str,
    ) -> CheckpointAssessmentClaim:
        """Claim execution once; a prior started/failed claim is never resumed."""
        checkpoint = uuid.UUID(checkpoint_id)
        assessor = checked_assessor(assessor)
        token = uuid.uuid4()
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO assessment.checkpoint_deliveries (checkpoint_id, assessor) "
                    "VALUES ($1, $2) ON CONFLICT (checkpoint_id, assessor) DO NOTHING",
                    checkpoint, assessor,
                )
                row = await conn.fetchrow(
                    "UPDATE assessment.checkpoint_deliveries SET status = 'started', "
                    "claim_token = $3, started_at = clock_timestamp() "
                    "WHERE checkpoint_id = $1 AND assessor = $2 AND status = 'pending' RETURNING *",
                    checkpoint, assessor, token,
                )
                claimed = row is not None
                if row is None:
                    row = await conn.fetchrow(
                        "SELECT * FROM assessment.checkpoint_deliveries WHERE checkpoint_id = $1 AND assessor = $2",
                        checkpoint, assessor,
                    )
                result = CheckpointAssessmentClaim(
                    checkpoint_id, assessor, row["status"],
                    str(row["claim_token"]) if claimed else None, claimed,
                )
            return result

    async def finish_checkpoint_assessment(
        self, *, checkpoint_id: str, assessor: str, claim_token: str, status: str,
    ) -> bool:
        """Record execution return/failure; no claim says all effects committed."""
        if status not in {"completed", "failed"}:
            raise ValueError("assessment terminal status must be completed or failed")
        checkpoint, token = uuid.UUID(checkpoint_id), uuid.UUID(claim_token)
        assessor = checked_assessor(assessor)
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE assessment.checkpoint_deliveries SET status = $4, finished_at = clock_timestamp() "
                "WHERE checkpoint_id = $1 AND assessor = $2 AND claim_token = $3 AND status = 'started' "
                "RETURNING status", checkpoint, assessor, token, status,
            )
            if row is not None:
                return True
            return bool(await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM assessment.checkpoint_deliveries "
                "WHERE checkpoint_id = $1 AND assessor = $2 AND claim_token = $3 AND status = $4)",
                checkpoint, assessor, token, status,
            ))
