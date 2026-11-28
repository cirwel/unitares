"""Neutral checkpoint capture at the authenticated check-in admission boundary.

The ledger is independent of assessment. This adapter retains synchronous EISV
execution and never repeats it when a key resolves to an existing checkpoint.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

from mcp.types import TextContent

from src.checkpoints import CheckpointConflict, CheckpointProvenance
from src.logging_utils import get_logger
from src.report_excerpt import make_report_excerpt, scrub_credentials

logger = get_logger(__name__)
ASSESSOR = "eisv-sync-v1"

# Versioned caller semantics, captured before inference, dampening or mutable
# monitor reads. Only the HMAC is stored; these are NOT assessor replay inputs.
_REQUEST_DEFAULTS = {
    "response_text": None, "complexity": 0.5, "confidence": None,
    "epistemic_class": "agent_report", "parameters": [],
    "ethical_drift": [0.0, 0.0, 0.0], "task_type": "mixed",
    "sensor_data": None, "recent_tool_results": None, "logprobs": None,
    "trajectory_signature": None,
    "provenance_context": {}, "comparison_key": None, "task_label": None,
    "task_outcome": None, "memory_context": None,
    "auto_export_on_significance": False,
}
_EXTRA_SEMANTICS = (
    "harness_type", "model_provider", "model", "transport", "tool_surface",
    "governance_mode", "verification_source", "locus", "runtime_provenance",
    "episode_fork_kind", "identity_lineage_fork", "_recovered_s22_context",
)
_SECRET_KEY = re.compile(r"(?:password|secret|token|api[_-]?key|session[_-]?(?:key|id))$", re.I)


def _scrub(value):
    if isinstance(value, str):
        return scrub_credentials(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_scrub(item) for item in value]
    return value


def capture_checkpoint_request(ctx) -> None:
    """Freeze only submitted semantics; response shape and proof are excluded."""
    key = ctx.arguments.get("idempotency_key")
    if key is not None and (
        not isinstance(key, str) or not key.strip() or len(key) > 200
    ):
        raise ValueError("idempotency_key must be a nonblank string of at most 200 characters")
    request = {name: ctx.arguments.get(name, default) for name, default in _REQUEST_DEFAULTS.items()}
    request.update({name: ctx.arguments.get(name) for name in _EXTRA_SEMANTICS})
    ctx.checkpoint_request = json.dumps(
        {"schema": "checkin.request.v1", "submitted": _scrub(request)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    ctx.checkpoint_key = key
    ctx.checkpoint_attempt_id = str(uuid4())


class CheckpointBoundaryStop(ValueError):
    """An admission outcome that must skip every downstream assessment effect."""

    def __init__(self, code: str, message: str, *, replay: bool = False):
        super().__init__(message)
        self.code = code
        self.replay = replay


async def accept_checkpoint(ctx, *, record_activity: bool = False) -> None:
    """Called by the authenticated service after its existing admission gates."""
    from src.db import get_db
    from src.db.mixins.identity import CheckpointIdentityRefused

    db = get_db()
    try:
        identity = await db.get_identity(ctx.agent_id)
        if identity is None:
            raise CheckpointBoundaryStop("CHECKPOINT_REFUSED", "No durable identity exists for this check-in.")
        if record_activity and ctx.meta is not None:
            # Set before the await: a lost ACK or cancellation may follow COMMIT.
            # A later request must refresh the cache even if mode changes.
            ctx.meta._checkpoint_activity_unconfirmed = True
        activity_options = {"record_activity": True} if record_activity else {}
        ctx.checkpoint_receipt = await db.accept_checkpoint(
            agent_id=ctx.agent_id,
            identity_id=identity.identity_id,
            idempotency_key=ctx.checkpoint_key,
            request_payload=ctx.checkpoint_request,
            report_excerpt=make_report_excerpt(ctx.response_text),
            provenance=CheckpointProvenance(
                epistemic_class=ctx.epistemic_class,
                identity_tier=ctx.identity_assurance.get("tier", "unknown"),
                proof_origin=ctx.proof_origin or "unknown",
            ),
            attempt_id=ctx.checkpoint_attempt_id,
            **activity_options,
        )
    except CheckpointConflict as exc:
        raise CheckpointBoundaryStop(
            "CHECKPOINT_KEY_CONFLICT",
            "This idempotency key is already bound to different submitted check-in content.",
        ) from exc
    except CheckpointIdentityRefused as exc:
        raise CheckpointBoundaryStop("CHECKPOINT_REFUSED", "Durable identity is not active and consistent.") from exc
    except CheckpointBoundaryStop:
        raise
    except Exception as exc:
        # COMMIT may have succeeded even when the acknowledgement was lost.
        ctx.checkpoint_unconfirmed = True
        raise CheckpointBoundaryStop(
            "CHECKPOINT_UNCONFIRMED",
            "Checkpoint acceptance could not be confirmed. Retry the same content with the same explicit idempotency key; without one, a retry may create another record.",
        ) from exc

    if record_activity:
        activity = ctx.checkpoint_receipt.activity
        if activity is not None:
            _apply_checkpoint_activity(ctx, activity)
        else:
            await refresh_checkpoint_activity(ctx)
    if ctx.checkpoint_receipt.replayed:
        # Reading the prior receipt never returns a current monitor's verdict,
        # prediction or metrics and never claims/delivers an assessment again.
        raise CheckpointBoundaryStop(
            "CHECKPOINT_REPLAY", "Returning the original accepted checkpoint; assessment was not repeated.", replay=True,
        )
    if isinstance(getattr(ctx, "agent_state", None), dict):
        ctx.agent_state["_checkpoint_id"] = ctx.checkpoint_receipt.checkpoint_id


def _apply_checkpoint_activity(ctx, activity) -> None:
    """Publish only a committed identity-owned activity snapshot to the cache."""
    if ctx.meta is None:
        return
    ctx.meta.total_updates = activity.total_updates
    ctx.meta.neutral_updates = activity.neutral_updates
    if activity.last_update is not None:
        ctx.meta.last_update = activity.last_update
    ctx.meta.recent_update_timestamps = list(activity.recent_update_timestamps)
    ctx.meta.recent_decisions = list(activity.recent_decisions)
    ctx.meta._checkpoint_activity_unconfirmed = False


async def refresh_checkpoint_activity(ctx) -> None:
    """Refresh loop inputs after a possibly committed neutral acceptance."""
    from src.db import get_db

    activity = await get_db().get_checkpoint_activity(ctx.agent_id)
    if activity is not None:
        _apply_checkpoint_activity(ctx, activity)


async def accept_before_assessment(ctx) -> None:
    """Retain the existing separate acceptance/claim boundary for EISV."""
    from src.db import get_db

    await accept_checkpoint(ctx)
    db = get_db()
    ctx.checkpoint_assessment = await db.claim_checkpoint_assessment(
        checkpoint_id=ctx.checkpoint_receipt.checkpoint_id, assessor=ASSESSOR,
    )
    if not ctx.checkpoint_assessment.claimed:
        raise CheckpointBoundaryStop(
            "CHECKPOINT_ASSESSMENT_NOT_CLAIMED",
            "Checkpoint accepted; this request did not acquire assessment delivery. No assessment was repeated.",
        )


async def finish_checkpoint_assessment(ctx, *, failed: bool = False) -> None:
    claim = getattr(ctx, "checkpoint_assessment", None)
    if claim is None or not claim.claimed:
        return
    from src.db import get_db

    status = "failed" if failed else "completed"
    try:
        confirmed = await get_db().finish_checkpoint_assessment(
            checkpoint_id=claim.checkpoint_id, assessor=ASSESSOR,
            claim_token=claim.claim_token, status=status,
        )
        ctx.checkpoint_assessment_status = status if confirmed else "unconfirmed"
    except Exception:
        logger.warning("Checkpoint assessment completion acknowledgement unavailable", exc_info=True)
        ctx.checkpoint_assessment_status = "unconfirmed"


def checkpoint_response(ctx) -> dict | None:
    receipt = getattr(ctx, "checkpoint_receipt", None)
    if receipt is None:
        if not getattr(ctx, "checkpoint_unconfirmed", False):
            return None
        return {
            "schema": "checkpoint.acceptance.v1", "status": "unconfirmed",
            "attempt_id": ctx.checkpoint_attempt_id,
            "acceptance_retry_idempotent": ctx.checkpoint_key is not None,
        }
    response = receipt.to_dict()
    if receipt.replayed:
        # The ledger does not attest to which mode originally accepted this.
        response["assessment"] = {"status": "not_repeated", "automatic_replay": False}
        return response
    if getattr(ctx, "checkin_assessor", "eisv") == "none":
        response["assessment"] = {
            "status": "not_requested", "reason": "disabled_by_operator",
            "automatic_replay": False,
        }
        return response
    claim = getattr(ctx, "checkpoint_assessment", None)
    response["assessment"] = {
        "assessor": ASSESSOR,
        "status": getattr(ctx, "checkpoint_assessment_status", None) or (
            claim.status if claim is not None else "unconfirmed"
        ),
        "automatic_replay": False,
        "completion_means": "execution_returned; effects_are_not_atomic",
    }
    return response


def checkpoint_boundary_response(ctx, *, code: str, message: str, replay: bool = False):
    """Primitive receipt-only payload, independent of estimator and formatter."""
    payload = {
        "success": replay, "agent_id": ctx.agent_id, "checkpoint": checkpoint_response(ctx),
        "assessment_repeated": False,
    }
    state_receipt = getattr(ctx, "state_persistence", None)
    if state_receipt is not None:
        payload["state_persistence"] = state_receipt.to_dict()
    if replay:
        payload["message"] = message
    else:
        payload.update(error_code=code, error=message)
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def checkpoint_recorded_response(ctx):
    """Fresh neutral acknowledgement with no assessment or policy verdict."""
    return [TextContent(type="text", text=json.dumps({
        "success": True, "agent_id": ctx.agent_id,
        "checkpoint": checkpoint_response(ctx), "assessment_repeated": False,
        "message": "Checkpoint and activity recorded; assessment disabled by operator configuration.",
    }, ensure_ascii=False))]
