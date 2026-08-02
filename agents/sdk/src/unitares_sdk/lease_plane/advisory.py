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
    LEASE_PLANE_ENFORCED_SURFACE_KINDS — comma-separated surface kinds
        promoted to Phase B enforcement. When a wrapped surface kind is
        listed, the block runs only after the lease is acquired.
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
    AcquireResult,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    LeasePlaneDisabledClient,
    ReleaseRequest,
    SimpleError,
    SimpleOk,
)
from .reclaim import ReclaimMemory

__all__ = [
    "AdvisoryOutcome",
    "LeaseEnforcementBlocked",
    "acquire_advisory",
    "enforced_surface_kinds",
    "is_surface_enforced",
    "lease_advisory_scope",
    "make_advisory_client",
    "new_holder_uuid",
    "release_advisory",
    "reset_reclaim_memory_for_tests",
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


class LeaseEnforcementBlocked(RuntimeError):
    """Raised when a Phase B promoted surface cannot acquire its lease."""

    def __init__(self, *, surface_id: str, outcome: AdvisoryOutcome) -> None:
        super().__init__(
            f"lease enforcement blocked surface_id={surface_id!r} outcome={outcome}"
        )
        self.surface_id = surface_id
        self.outcome = outcome


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
 `force_new` semantics from ``. Long-lived
    residents that want substrate-earned continuity will graduate later.
    """
    return uuid.uuid4()


def enforced_surface_kinds(raw: str | None = None) -> frozenset[str]:
    """Return the configured Phase B enforced surface kinds.

    The env value is comma-separated (`resident,file`). Empty items are
    ignored. Use `*` only for explicit all-surface local testing.
    """
    value = raw if raw is not None else os.environ.get("LEASE_PLANE_ENFORCED_SURFACE_KINDS", "")
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _surface_kind(surface_id: str) -> str:
    return surface_id.split(":", 1)[0]


def is_surface_enforced(surface_id: str) -> bool:
    """Return whether `surface_id` is promoted to Phase B enforcement."""
    configured = enforced_surface_kinds()
    return "*" in configured or _surface_kind(surface_id) in configured


@contextlib.contextmanager
def lease_advisory_scope(
    *,
    surface_id: str,
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

    `surface_kind` was a parameter pre-PR-2.5 but has been fully removed —
    per RFC v0.8 §7.2.3, surface_kind is derived server-side from the
    surface_id scheme prefix via migration 026's generated column.
    """
    advisory_client = client or make_advisory_client()

    request = AcquireRequest(
        surface_id=surface_id,
        holder_agent_uuid=holder_agent_uuid,
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=ttl_s,
        intent=intent,
        audit_session=audit_session,
    )

    outcome, lease_id = acquire_advisory(advisory_client, request)
    if lease_id is None and is_surface_enforced(surface_id):
        logger.warning(
            "lease_enforcement: blocked surface=%s outcome=%s",
            surface_id,
            outcome,
        )
        raise LeaseEnforcementBlocked(surface_id=surface_id, outcome=outcome)

    try:
        yield outcome, lease_id
    finally:
        if lease_id is not None:
            release_advisory(advisory_client, lease_id)


# Process-wide reclaim memory (issue #1460, port of #1459's LeaseReclaim).
# Module-level so every advisory caller in this process shares one view of
# the uuids it has put on the wire; per-surface keying lives inside.
_reclaim_memory = ReclaimMemory()


def reset_reclaim_memory_for_tests() -> None:
    """Swap in a fresh reclaim store — for unit-test isolation only."""
    global _reclaim_memory
    _reclaim_memory = ReclaimMemory()


def _transport_failed(result: object) -> bool:
    """True when the request may have COMMITTED server-side unseen.

    Only the transport-exception discriminant qualifies: a server-sent 503
    or a disabled client is a received answer, not a lost one.
    """
    return (
        isinstance(result, AcquireServiceUnavailable)
        and result.reason == "transport_exception"
    )


def _release_reclaimed(client: LeasePlaneClient, lease_id: uuid.UUID) -> bool:
    """Release our own stranded lease; report whether the surface is free.

    Unlike the scope's exit-time release (best-effort, swallows everything),
    a reclaim release must report whether it worked: on failure the caller
    keeps its candidate memory and retries on a later attempt instead of
    assuming the orphan is gone.

    Fallback to ``release_reason='normal'`` happens ONLY on a 422
    schema_invalid — the one unambiguous signal that this plane's router
    predates ``reclaimed_lost_acquire``. Deliberately NOT applied to
    transport errors (a timed-out release may still have committed with the
    right reason; a 'normal' retry would mislabel exactly the orphan spans
    this reason exists to distinguish) nor to 503s (ambiguous between a real
    internal error and new router code over an unapplied 056 migration —
    failing loudly routes the operator to the missing migration instead of
    silently self-defeating the label forever).
    """
    try:
        result = client.release(
            ReleaseRequest(lease_id=lease_id, release_reason="reclaimed_lost_acquire")
        )
    except Exception as exc:  # defensive — client is supposed to be no-raise
        logger.warning(
            "lease_advisory: reclaim release raised lease_id=%s err=%r", lease_id, exc
        )
        return False

    if isinstance(result, SimpleOk):
        logger.info(
            "lease_advisory: released reclaimed lease_id=%s reason=reclaimed_lost_acquire",
            lease_id,
        )
        return True

    if isinstance(result, SimpleError) and result.error == "not_found":
        # Already gone (operator force-release or reaper racing us) — the
        # reclaim's goal is achieved.
        logger.info(
            "lease_advisory: reclaim target lease_id=%s already released — proceeding",
            lease_id,
        )
        return True

    if isinstance(result, SimpleError) and result.error == "schema_invalid":
        fallback = client.release(
            ReleaseRequest(lease_id=lease_id, release_reason="normal")
        )
        if isinstance(fallback, SimpleOk) or (
            isinstance(fallback, SimpleError) and fallback.error == "not_found"
        ):
            logger.info(
                "lease_advisory: released reclaimed lease_id=%s reason=normal "
                "(old-plane fallback: router predates reclaimed_lost_acquire)",
                lease_id,
            )
            return True
        logger.warning(
            "lease_advisory: reclaim old-plane fallback failed lease_id=%s result=%r",
            lease_id,
            fallback,
        )
        return False

    logger.warning(
        "lease_advisory: reclaim release failed lease_id=%s result=%r — "
        "will retry on a later attempt",
        lease_id,
        result,
    )
    return False


def acquire_advisory(
    client: LeasePlaneClient,
    request: AcquireRequest,
    *,
    memory: ReclaimMemory | None = None,
    _allow_reclaim: bool = True,
) -> tuple[AdvisoryOutcome, uuid.UUID | None]:
    """Acquire a Phase A advisory lease and classify the outcome.

    Public counterpart to `lease_advisory_scope` for callers that need the
    acquire-without-context-manager shape (e.g., bash glue calling a CLI).
    Always non-fatal — never raises from the lease layer.

    Lost-response recovery (issue #1460, port of the #1459 design):

    1. When the transport raises, retry ONCE with the SAME body — the first
       attempt may have committed server-side, and the plane's acquire is
       idempotent on (surface_id, holder_agent_uuid), so the retry either
       returns the committed lease or performs the acquire (#1443
       equivalent).
    2. Every holder uuid put on the wire is remembered per surface —
       transport-failed attempts AND successful acquires (`ReclaimMemory`).
    3. A later `held_by_other` naming a remembered uuid is our own lease,
       stranded by a lost acquire (or lost release) response: release it
       with `release_reason='reclaimed_lost_acquire'` and re-acquire once
       with a fresh uuid, in this same call.

    `memory` is injectable for tests; production callers share the module
    store. `_allow_reclaim` guards the nested re-acquire against recursion.
    """
    store = memory if memory is not None else _reclaim_memory
    holder_uuid = str(request.holder_agent_uuid)

    def _attempt() -> AcquireResult | None:
        try:
            return client.acquire(request)
        except Exception as exc:  # defensive — client is supposed to be no-raise
            logger.warning(
                "lease_advisory: acquire raised unexpectedly surface=%s err=%r",
                request.surface_id,
                exc,
            )
            return None

    result = _attempt()
    if result is None:
        return "client_error", None

    response_lost = False
    if _transport_failed(result):
        # The response is lost: the attempt MAY have committed server-side.
        # Retry once with the same body — the plane's acquire is idempotent
        # on (surface_id, holder_agent_uuid).
        response_lost = True
        logger.warning(
            "lease_advisory: acquire response lost surface=%s — retrying once "
            "with the same holder uuid (idempotent server-side)",
            request.surface_id,
        )
        result = _attempt()

    # Record the wire uuid EXACTLY ONCE, by final outcome: a recovered retry
    # is a successful acquire (recording it as an attempt too would let the
    # acquire's absence-stamp mark its own earlier entry proven and burn a
    # second backstop slot); any other outcome after a lost response leaves
    # a possibly-committed lease behind, so the uuid must be remembered
    # (harmless if it never committed — a held_by_other can only name it if
    # the server actually granted it).
    if isinstance(result, AcquireOk):
        store.absorb(request.surface_id, acquired_holder_uuid=holder_uuid)
    elif response_lost:
        store.absorb(request.surface_id, attempted_holder_uuid=holder_uuid)

    if result is None:
        return "client_error", None

    if (
        _allow_reclaim
        and isinstance(result, AcquireHeldByOther)
        and str(result.held_by_uuid) in store.candidates(request.surface_id)
    ):
        logger.warning(
            "lease_advisory: held_by_other names our own prior attempt — "
            "reclaiming lease stranded by a lost response (surface=%s "
            "lease_id=%s holder_uuid=%s)",
            request.surface_id,
            result.blocking_lease_id,
            result.held_by_uuid,
        )
        if _release_reclaimed(client, result.blocking_lease_id):
            retry_request = request.model_copy(
                update={"holder_agent_uuid": new_holder_uuid()}
            )
            return acquire_advisory(
                client, retry_request, memory=store, _allow_reclaim=False
            )
        # Release failed — keep the candidate (absorb never cleared it) and
        # surface the conflict; a later attempt retries the reclaim.

    return _classify_acquire(result, request)


def _classify_acquire(
    result: AcquireResult, request: AcquireRequest
) -> tuple[AdvisoryOutcome, uuid.UUID | None]:
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


def release_advisory(client: LeasePlaneClient, lease_id: uuid.UUID) -> None:
    """Release a Phase A advisory lease, swallowing any error.

    Public counterpart to the context manager's exit-time release. Logs
    the outcome but never raises — Phase A cleanup is best-effort.

    One transport retry (addition beyond the #1459 port, flagged in #1460's
    PR): for one-shot CLI callers the in-memory reclaim dies with the
    process, so a lost release response strands the lease until an operator
    notices (the 2026-08-01 ship.sh orphan). Release is idempotent
    server-side (``WHERE released_at IS NULL``), so retrying the same
    request once is safe — a duplicate of a release that actually committed
    is a no-op ``not_found``/``already_released``.
    """
    try:
        request = ReleaseRequest(lease_id=lease_id, release_reason="normal")
        result = client.release(request)
        if isinstance(result, SimpleError) and result.reason == "transport_exception":
            logger.warning(
                "lease_advisory: release response lost lease_id=%s — retrying "
                "once (idempotent server-side)",
                lease_id,
            )
            result = client.release(request)
        logger.info(
            "lease_advisory: released lease_id=%s ok=%s",
            lease_id,
            getattr(result, "ok", None),
        )
    except Exception as exc:
        logger.warning("lease_advisory: release raised lease_id=%s err=%r", lease_id, exc)
