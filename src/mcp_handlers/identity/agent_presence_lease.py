"""Producer side of the ephemeral-agent liveness lease.

On the onboard and check-in paths, keep a fresh ``agent:/<uuid>``
remote_heartbeat presence lease in the lease plane so the archival gate
(``has_live_agent_lease``, the
consumer in ``process_binding``) can tell a live ephemeral agent from an exited
one. This is what makes the #720 false-archival protection real for ephemeral
agents — which today write no process binding, so binding-liveness is blind to
them.

Why both paths: substrate-agnostic liveness invariants belong on the check-in
path, because BEAM residents and raw-MCP agents bypass onboard. But a live MCP
agent can do accountable work between successful onboard and first check-in, so
onboard must acquire the initial lease too. Later check-ins heartbeat it.

Safety / non-interference:
  * Fire-and-forget and best-effort — a lease failure must NEVER affect the
    check-in (the caller schedules this via create_tracked_task and ignores it).
  * No-ops silently when the lease plane is not in scope for this deploy
    (``LEASE_PLANE_BEARER_TOKEN`` unset) or the SDK is unavailable.
  * The heartbeat is a raw HTTP side-effect to the lease plane. It deliberately
    does NOT route through any governance-tool / check-in / activity path, so it
    cannot feed loop-detection or the auto-heartbeat activity tracker (the Dec
    reply_to_question/dialectic false-positive class). It is a lease side-effect,
    not a high-impact agent action.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# TTL for the presence lease. Must exceed the typical inter-check-in gap (so a
# working agent's lease stays fresh between turns) and is bounded by the lease
# plane's 3600s cap. 600s balances "survives a normal check-in gap" against "a
# dead agent's row self-heals (TTL-reaped) within ~10 min".
_PRESENCE_TTL_S = 600

# In-process uuid -> lease_id cache. Acquire once, heartbeat after; on heartbeat
# failure (expired / reaped) drop it and re-acquire. Lost on restart -> simply
# re-acquired on the next check-in. Never authoritative — the lease-plane row is
# the source of truth.
_lease_ids: dict[str, str] = {}

# uuid -> {client_session_id: monotonic time of its last acquire/refresh} for
# the cached lease. Several sessions can share one identity's lease (a resume
# overlapping a slow exit); a release removes only the releasing session and
# frees the lease only when no other session has refreshed it within the TTL.
# A refresh without a session id is recorded under _HOLDER_UNKNOWN.
#
# Boundary: client_session_id is derived from the identity, so two processes
# live under one uuid at the same time share a key here and cannot be told
# apart. The identity contract rules that state out (a fresh process mints a
# fresh identity; a continuity_token rebind is same-live-process only), so this
# bookkeeping does not try to separate them.
_lease_sessions: dict[str, dict[str, float]] = {}

# uuid -> monotonic time of the agent's own clean-exit release, and the
# client_session_id(s) that released it. A heartbeat from the releasing session
# never re-acquires, however late it lands (a host's final check-in can reach
# the server after its session-end release); nor does any heartbeat scheduled
# before the release. A different session resuming the same identity after the
# release proceeds normally.
_released_at: dict[str, float] = {}
_released_sessions: dict[str, set[str]] = {}

# How long a release suppresses the released session's heartbeats. The race it
# covers is a host's final check-in landing just after its session-end release,
# which takes seconds. client_session_id is derived from the identity
# (make_client_session_id), so a later rebind of the same identity carries the
# same id; past this window its heartbeats must proceed, or a live agent would
# lose its presence and read as exited.
_RELEASE_SUPPRESS_S = 120.0


def _expire_tombstone(agent_uuid: str, now: float) -> None:
    released = _released_at.get(agent_uuid)
    if released is not None and now - released > _RELEASE_SUPPRESS_S:
        _released_at.pop(agent_uuid, None)
        _released_sessions.pop(agent_uuid, None)

# Per-uuid lock shared by heartbeat and release. Without it a resumed session's
# heartbeat can race an in-progress release: it finds the cache emptied while
# the old row is still live, gets held_by_other, and is left with no lease.
_locks: dict[str, asyncio.Lock] = {}


_SWEEP_INTERVAL_S = 60.0
_last_sweep = 0.0


def _sweep(now: float) -> None:
    """Drop per-identity state nothing still refreshes.

    Every fresh process mints a fresh identity, so without this the lock, holder
    and cache maps grow for the life of the server. An identity whose holders
    have all been silent past the TTL has an expired lease anyway; a later
    heartbeat simply re-acquires. A lock is dropped only when nobody holds it
    (an asyncio.Lock has no waiters unless it is held).
    """
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    for agent_uuid, holders in list(_lease_sessions.items()):
        if all(now - seen > _PRESENCE_TTL_S for seen in holders.values()):
            _lease_sessions.pop(agent_uuid, None)
            _lease_ids.pop(agent_uuid, None)
    for agent_uuid in [u for u, at in _released_at.items() if now - at > _RELEASE_SUPPRESS_S]:
        _released_at.pop(agent_uuid, None)
        _released_sessions.pop(agent_uuid, None)
    for agent_uuid, lock in list(_locks.items()):
        if (
            not lock.locked()
            and agent_uuid not in _lease_sessions
            and agent_uuid not in _released_at
        ):
            _locks.pop(agent_uuid, None)


def _lock_for(agent_uuid: str) -> asyncio.Lock:
    lock = _locks.get(agent_uuid)
    if lock is None:
        lock = _locks[agent_uuid] = asyncio.Lock()
    return lock

# Guarded SDK imports: unavailable in isolated test/CI envs and in deploys
# without the lease-plane boundary. When absent the module loads and every entry
# point no-ops. Tests monkeypatch these module attributes with fakes.
try:  # pragma: no cover - import availability is environment-dependent
    from src.lease_plane import LeasePlaneClient, LeasePlaneClientConfig
    from unitares_sdk.lease_plane.models import (
        AcquireRequest,
        HeartbeatRequest,
        ReleaseRequest,
    )
except Exception:  # pragma: no cover
    LeasePlaneClient = None  # type: ignore
    LeasePlaneClientConfig = None  # type: ignore
    AcquireRequest = None  # type: ignore
    HeartbeatRequest = None  # type: ignore
    ReleaseRequest = None  # type: ignore


def _make_client():
    """Return a configured LeasePlaneClient, or None when the lease plane is not
    in scope for this deploy (no bearer token / SDK unavailable)."""
    if LeasePlaneClient is None or LeasePlaneClientConfig is None:
        return None
    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not bearer:
        return None
    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788")
    try:
        return LeasePlaneClient(
            LeasePlaneClientConfig(base_url=base_url, bearer_token=bearer, timeout_s=5.0)
        )
    except Exception:  # pragma: no cover - defensive
        return None


def _released_since(
    agent_uuid: str,
    scheduled_at: Optional[float],
    client_session_id: Optional[str] = None,
) -> bool:
    """True when this heartbeat belongs to a session that has just exited."""
    _expire_tombstone(agent_uuid, time.monotonic())
    released = _released_at.get(agent_uuid)
    if released is None:
        return False
    if client_session_id:
        # A heartbeat that names its session is judged by that alone: the
        # releasing session is suppressed however late it lands, and any other
        # session (a resume) proceeds even if it was queued before the release.
        return client_session_id in _released_sessions.get(agent_uuid, ())
    return scheduled_at is not None and released >= scheduled_at


async def heartbeat_agent_presence(
    agent_uuid: Optional[str],
    client_session_id: Optional[str] = None,
    scheduled_at: Optional[float] = None,
) -> None:
    """Keep the ``agent:/<uuid>`` presence lease fresh. Fire-and-forget; never raises."""
    if not agent_uuid:
        return
    _sweep(time.monotonic())
    if _released_since(agent_uuid, scheduled_at, client_session_id):
        return
    client = _make_client()
    if client is None:
        return
    try:
        async with _lock_for(agent_uuid):
            if _released_since(agent_uuid, scheduled_at, client_session_id):
                return
            await _refresh_presence(client, agent_uuid, client_session_id, scheduled_at)
    except Exception as e:  # pragma: no cover - best-effort; must never affect check-in
        logger.debug(f"[AGENT_PRESENCE] heartbeat_agent_presence failed (non-fatal): {e}")


async def _refresh_presence(
    client,
    agent_uuid: str,
    client_session_id: Optional[str],
    scheduled_at: Optional[float] = None,
) -> None:
    """Heartbeat the cached lease, or (re)acquire one. Manages the lease_id cache."""
    loop = asyncio.get_running_loop()

    cached = _lease_ids.get(agent_uuid)
    if cached is not None and HeartbeatRequest is not None:
        try:
            heartbeat_request = HeartbeatRequest(lease_id=cached)
            identity_proof = _mint_presence_attestation(
                agent_uuid, "/v1/lease/heartbeat", heartbeat_request
            )
            result = await loop.run_in_executor(
                None,
                lambda: client.heartbeat(
                    heartbeat_request,
                    identity_proof=identity_proof,
                ),
            )
            if getattr(result, "ok", False):
                # A nameless refresh proves someone is live without saying who,
                # so no session's release may free it.
                _lease_sessions.setdefault(agent_uuid, {})[
                    client_session_id or _HOLDER_UNKNOWN
                ] = time.monotonic()
                return
        except Exception:
            pass
        # Expired, reaped, refused, or unknown lease_id — drop and re-acquire.
        _lease_ids.pop(agent_uuid, None)

    if AcquireRequest is None:
        return
    acquire_request = AcquireRequest(
        surface_id=f"agent:/{agent_uuid}",
        holder_agent_uuid=agent_uuid,
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=_PRESENCE_TTL_S,
        audit_session=client_session_id,
    )
    identity_proof = _mint_presence_attestation(
        agent_uuid, "/v1/lease/acquire", acquire_request
    )
    result = await loop.run_in_executor(
        None,
        lambda: client.acquire(acquire_request, identity_proof=identity_proof),
    )
    # The SDK's AcquireOk nests the id in its lease record (result.lease.lease_id);
    # failure variants (held_by_other, etc.) carry none. Reading only a flat
    # result.lease_id left the id uncached, so every heartbeat re-acquired.
    new_id = getattr(result, "lease_id", None) or getattr(
        getattr(result, "lease", None), "lease_id", None
    )
    if new_id:
        # Idempotent: the identity already held this lease, so other sessions
        # may be sharing it.
        idempotent = bool(getattr(result, "idempotent", False))
        if _released_since(agent_uuid, scheduled_at, client_session_id):
            # The session ended while this acquire was in flight. Hand a lease
            # it created straight back; never one another session shares.
            if not idempotent:
                await _release_lease(client, agent_uuid, str(new_id))
            return
        _lease_ids[agent_uuid] = str(new_id)
        holder = client_session_id or _HOLDER_UNKNOWN
        if idempotent:
            holders = _lease_sessions.setdefault(agent_uuid, {})
            if not holders:
                # Cold cache (e.g. after a restart): seed from the persisted
                # record so the session that already holds the lease counts.
                persisted = _persisted_holder(getattr(result, "lease", None))
                if persisted and persisted not in _released_sessions.get(agent_uuid, ()):
                    holders[persisted] = time.monotonic()
            holders[holder] = time.monotonic()
        else:
            # A fresh lease starts a fresh holder set; the old one expired with it.
            _lease_sessions[agent_uuid] = {holder: time.monotonic()}


def _mint_presence_attestation(agent_uuid: str, path: str, request: object) -> str | None:
    """Mint a request-bound proof after onboarding has established the UUID.

    This runs inside governance, where the operator signing key lives.  It
    avoids the bootstrap loop that would result from asking an onboarding call
    to present the continuity token it has not returned yet.
    """
    try:
        payload = request.model_dump(mode="json", exclude_none=True)  # type: ignore[attr-defined]
        wire_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        from src.lease_attestation import mint_lease_attestation

        return mint_lease_attestation(
            holder_agent_uuid=agent_uuid,
            method="POST",
            path=path,
            body_sha256=hashlib.sha256(wire_body).hexdigest(),
        )
    except Exception as exc:  # noqa: BLE001 — presence maintenance is best-effort
        logger.debug(
            "[AGENT_PRESENCE] lease attestation unavailable (non-fatal): %s",
            type(exc).__name__,
        )
        return None


def schedule_agent_presence_heartbeat(
    agent_uuid: Optional[str], client_session_id: Optional[str] = None
) -> None:
    """Schedule a best-effort presence heartbeat without affecting caller flow."""
    try:
        if not agent_uuid:
            return
        from src.background_tasks import create_tracked_task

        create_tracked_task(
            heartbeat_agent_presence(agent_uuid, client_session_id, time.monotonic()),
            name="agent_presence_lease",
        )
    except Exception as e:  # pragma: no cover - scheduling must never affect callers
        logger.debug(f"[AGENT_PRESENCE] scheduling skipped: {e}")


# Holder marker for a lease whose current session cannot be determined.
_HOLDER_UNKNOWN = "\x00unknown"


def _persisted_holder(record) -> Optional[str]:
    """The session a lease record names as holder, or unknown once renewed.

    The record keeps the acquiring session's audit_session, and a renewal does
    not update it, so after any renewal the current holder cannot be named."""
    if record is None:
        return None
    acquired = getattr(record, "acquired_at", None)
    last_heartbeat = getattr(record, "last_heartbeat_at", None)
    if last_heartbeat is not None and (acquired is None or last_heartbeat > acquired):
        return _HOLDER_UNKNOWN
    return getattr(record, "audit_session", None) or _HOLDER_UNKNOWN


async def _lookup_live_lease(agent_uuid: str) -> tuple[Optional[str], Optional[str]]:
    """Find the agent's unreleased presence lease when the in-process cache lost
    it (a server restart since the acquire), with the session known to hold it.

    The row records the acquiring session, but a renewal does not update it, so
    once the lease has been renewed its current holder is unknown and the
    caller must leave it to the TTL. Returns (None, None) on any error."""
    try:
        from src.db import get_db

        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT lease_id::text AS lease_id, audit_session,
                       (last_heartbeat_at IS NOT NULL
                        AND last_heartbeat_at > acquired_at) AS renewed
                FROM lease_plane.surface_leases
                WHERE surface_id = $1
                  AND released_at IS NULL
                  AND expires_at > NOW()
                ORDER BY expires_at DESC
                LIMIT 1
                """,
                f"agent:/{agent_uuid}",
            )
        if not row or not row["lease_id"]:
            return None, None
        # A null audit_session was a nameless acquire: its caller cannot be
        # named, so it counts as an unknown live holder, as a renewal does.
        holder = _HOLDER_UNKNOWN if row["renewed"] else (row["audit_session"] or _HOLDER_UNKNOWN)
        return str(row["lease_id"]), holder
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[AGENT_PRESENCE] live lease lookup failed (non-fatal): {e}")
        return None, None


async def _release_lease(client, agent_uuid: str, lease_id: str) -> bool:
    if ReleaseRequest is None:
        return False
    loop = asyncio.get_running_loop()
    release_request = ReleaseRequest(lease_id=lease_id, release_reason="normal")
    identity_proof = _mint_presence_attestation(
        agent_uuid, "/v1/lease/release", release_request
    )
    result = await loop.run_in_executor(
        None,
        lambda: client.release(release_request, identity_proof=identity_proof),
    )
    return bool(getattr(result, "ok", False))


async def release_agent_presence(
    agent_uuid: Optional[str], client_session_ids: tuple[str, ...] = ()
) -> dict:
    """Release the agent's own presence lease on a clean exit.

    Without this, an exited agent reads as live for up to ``_PRESENCE_TTL_S``,
    and a successor that declares it as parent inside that window is refused as
    co-located (``lineage_coincidental_rejected``). A crash still falls back to
    the TTL. Never raises; the result says what happened.
    """
    if not agent_uuid:
        return {"released": False, "reason": "no_identity"}
    _sweep(time.monotonic())
    session_ids = tuple(s for s in client_session_ids if s)
    if not session_ids:
        # Without the releasing session's id, a late final check-in from that
        # session could not be told apart from a resumed session and would
        # re-acquire the lease. Leave the TTL in charge instead.
        return {"released": False, "reason": "session_id_required"}
    async with _lock_for(agent_uuid):
        # Tombstone inside the lock: a heartbeat already in flight finishes and
        # caches its lease first, so the release below finds and frees it.
        now = time.monotonic()
        for stale in [u for u, at in _released_at.items() if now - at > _RELEASE_SUPPRESS_S]:
            _released_at.pop(stale, None)
            _released_sessions.pop(stale, None)
        _released_at[agent_uuid] = now
        sessions = _released_sessions.setdefault(agent_uuid, set())
        sessions.update(session_ids)

        # The releasing session is gone whatever happens below.
        holders = _lease_sessions.setdefault(agent_uuid, {})
        for session_id in session_ids:
            holders.pop(session_id, None)

        client = _make_client()
        if client is None:
            # Keep the cached lease id so a retry can still release it.
            return {"released": False, "reason": "lease_plane_unavailable"}
        lease_id = _lease_ids.get(agent_uuid)
        if not lease_id:
            lease_id, db_holder = await _lookup_live_lease(agent_uuid)
            if db_holder and db_holder not in sessions:
                holders[db_holder] = now
        if not lease_id:
            return {"released": False, "reason": "no_live_lease"}
        others = {s for s, seen in holders.items() if now - seen <= _PRESENCE_TTL_S}
        if others:
            # Another session under this identity is still refreshing the
            # lease (or one we cannot name is); its presence stays.
            reason = "holder_unknown" if others == {_HOLDER_UNKNOWN} else "held_by_other_session"
            return {"released": False, "reason": reason}
        try:
            ok = await _release_lease(client, agent_uuid, lease_id)
        except Exception as e:  # noqa: BLE001 - best-effort; the TTL remains the backstop
            logger.debug(f"[AGENT_PRESENCE] release failed (non-fatal): {e}")
            ok = False
        if ok:
            _lease_ids.pop(agent_uuid, None)
            _lease_sessions.pop(agent_uuid, None)
        # On a refused release the cached id stays for a retry.
        return {"released": ok, "reason": "released" if ok else "release_refused"}
