"""Phase A advisory-mode wrappers around the lease plane.

Per RFC v0.5 §6.1, Phase A integrations call the lease plane for
*telemetry only* — never enforcement. A failed acquire (held_by_other,
service_unavailable, network error, missing bearer token) MUST NOT block
the caller's normal operation. The point of Phase A is to discover
whether leases would have prevented real collisions, not to actually
prevent them yet.

This module is the recommended on-ramp for Python residents (Watcher,
Steward, ship.sh, dispatch). Each resident imports `lease_advisory_scope`
and wraps its unit-of-work without changing behavior on lease outcome.

Environment:
    LEASE_PLANE_BEARER_TOKEN — bearer token (sourced from
        ~/.config/cirwel/secrets.env). If unset, the helper returns a
        disabled client and every acquire surfaces as service_unavailable
        in the log; the caller proceeds normally.
    LEASE_PLANE_BASE_URL — defaults to http://127.0.0.1:8788.
"""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from collections.abc import Iterator
from typing import Literal

from . import (
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    LeasePlaneDisabledClient,
    ReleaseRequest,
)

__all__ = [
    "AdvisoryOutcome",
    "lease_advisory_scope",
    "make_advisory_client",
    "new_holder_uuid",
]

logger = logging.getLogger(__name__)


AdvisoryOutcome = Literal[
    "acquired_new",
    "acquired_idempotent",
    "held_by_other",
    "service_unavailable",
    "permission_denied",
    "schema_invalid",
    "client_error",
]


def make_advisory_client() -> LeasePlaneClient:
    """Construct the advisory-mode client.

    If `LEASE_PLANE_BEARER_TOKEN` is unset or empty, returns a
    `LeasePlaneDisabledClient` — every call returns `service_unavailable`,
    which is exactly what Phase A wants for unconfigured environments.
    """
    token = os.environ.get("LEASE_PLANE_BEARER_TOKEN", "").strip()
    base_url = os.environ.get("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").strip()

    if not token:
        return LeasePlaneDisabledClient()

    return LeasePlaneClient(
        LeasePlaneClientConfig(
            base_url=base_url,
            bearer_token=token,
            timeout_s=2.0,
        )
    )


def new_holder_uuid() -> uuid.UUID:
    """Fresh UUID for a Phase A holder.

    Phase A treats every Python invocation as a fresh process_instance —
    `force_new` semantics from `docs/ontology/identity.md`. Long-lived
    residents that want substrate-earned continuity will graduate later.
    """
    return uuid.uuid4()


@contextlib.contextmanager
def lease_advisory_scope(
    *,
    surface_id: str,
    surface_kind: str,
    holder_agent_uuid: uuid.UUID,
    ttl_s: int,
    intent: str | None = None,
    audit_session: str | None = None,
    client: LeasePlaneClient | None = None,
) -> Iterator[tuple[AdvisoryOutcome, uuid.UUID | None]]:
    """Phase A advisory wrapper.

    Yields `(outcome, lease_id_or_none)`. The yielded lease_id is set only
    on `acquired_new` or `acquired_idempotent`; on every other outcome the
    block still runs (Phase A is non-enforcing), but no release is issued
    on exit.

    The wrapper NEVER raises from the lease layer. Any exception raised by
    the caller's block will propagate normally; the wrapper only ensures
    the lease is released if it was acquired.
    """
    advisory_client = client or make_advisory_client()

    request = AcquireRequest(
        surface_id=surface_id,
        surface_kind=surface_kind,
        holder_agent_uuid=holder_agent_uuid,
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=ttl_s,
        intent=intent,
        audit_session=audit_session,
    )

    outcome, lease_id = _acquire_and_classify(advisory_client, request)

    try:
        yield outcome, lease_id
    finally:
        if lease_id is not None:
            _release_quiet(advisory_client, lease_id)


def _acquire_and_classify(
    client: LeasePlaneClient, request: AcquireRequest
) -> tuple[AdvisoryOutcome, uuid.UUID | None]:
    try:
        result = client.acquire(request)
    except Exception as exc:  # defensive — client is supposed to be no-raise
        logger.warning(
            "lease_advisory: acquire raised unexpectedly surface=%s err=%r",
            request.surface_id,
            exc,
        )
        return "client_error", None

    if isinstance(result, AcquireOk):
        outcome: AdvisoryOutcome = "acquired_idempotent" if result.idempotent else "acquired_new"
        logger.info(
            "lease_advisory: %s surface=%s lease_id=%s drift=%s",
            outcome,
            request.surface_id,
            result.lease.lease_id,
            result.drift_warning,
        )
        return outcome, result.lease.lease_id

    if isinstance(result, AcquireHeldByOther):
        logger.info(
            "lease_advisory: held_by_other surface=%s held_by=%s expires=%s "
            "(Phase A: proceeding regardless)",
            request.surface_id,
            result.held_by_uuid,
            result.expires_at.isoformat(),
        )
        return "held_by_other", None

    if isinstance(result, AcquireServiceUnavailable):
        logger.info(
            "lease_advisory: service_unavailable surface=%s "
            "(lease plane down or unconfigured)",
            request.surface_id,
        )
        return "service_unavailable", None

    if isinstance(result, AcquirePermissionDenied):
        logger.warning(
            "lease_advisory: permission_denied surface=%s reason=%s",
            request.surface_id,
            result.reason,
        )
        return "permission_denied", None

    if isinstance(result, AcquireSchemaInvalid):
        logger.warning(
            "lease_advisory: schema_invalid surface=%s detail=%s",
            request.surface_id,
            result.detail,
        )
        return "schema_invalid", None

    logger.warning(
        "lease_advisory: unrecognized result surface=%s result=%r",
        request.surface_id,
        result,
    )
    return "client_error", None


def _release_quiet(client: LeasePlaneClient, lease_id: uuid.UUID) -> None:
    try:
        result = client.release(ReleaseRequest(lease_id=lease_id, release_reason="normal"))
        logger.info(
            "lease_advisory: released lease_id=%s ok=%s",
            lease_id,
            getattr(result, "ok", None),
        )
    except Exception as exc:
        logger.warning("lease_advisory: release raised lease_id=%s err=%r", lease_id, exc)
