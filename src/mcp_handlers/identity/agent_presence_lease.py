"""Producer side of the ephemeral-agent liveness lease.

On the check-in path, keep a fresh ``agent:/<uuid>`` remote_heartbeat presence
lease in the lease plane so the archival gate (``has_live_agent_lease``, the
consumer in ``process_binding``) can tell a live ephemeral agent from an exited
one. This is what makes the #720 false-archival protection real for ephemeral
agents — which today write no process binding, so binding-liveness is blind to
them.

Why the check-in path (``process_agent_update``), not onboard: substrate-agnostic
liveness invariants belong on the check-in path. BEAM residents and raw-MCP
agents bypass onboard, but everything that is alive checks in. First check-in
acquires the lease; later check-ins heartbeat it.

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
import os
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

# Guarded SDK imports: unavailable in isolated test/CI envs and in deploys
# without the lease-plane boundary. When absent the module loads and every entry
# point no-ops. Tests monkeypatch these module attributes with fakes.
try:  # pragma: no cover - import availability is environment-dependent
    from src.lease_plane import LeasePlaneClient, LeasePlaneClientConfig
    from unitares_sdk.lease_plane.models import AcquireRequest, HeartbeatRequest
except Exception:  # pragma: no cover
    LeasePlaneClient = None  # type: ignore
    LeasePlaneClientConfig = None  # type: ignore
    AcquireRequest = None  # type: ignore
    HeartbeatRequest = None  # type: ignore


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


async def heartbeat_agent_presence(
    agent_uuid: Optional[str], client_session_id: Optional[str] = None
) -> None:
    """Keep the ``agent:/<uuid>`` presence lease fresh. Fire-and-forget; never raises."""
    if not agent_uuid:
        return
    client = _make_client()
    if client is None:
        return
    try:
        await _refresh_presence(client, agent_uuid, client_session_id)
    except Exception as e:  # pragma: no cover - best-effort; must never affect check-in
        logger.debug(f"[AGENT_PRESENCE] heartbeat_agent_presence failed (non-fatal): {e}")


async def _refresh_presence(client, agent_uuid: str, client_session_id: Optional[str]) -> None:
    """Heartbeat the cached lease, or (re)acquire one. Manages the lease_id cache."""
    loop = asyncio.get_running_loop()

    cached = _lease_ids.get(agent_uuid)
    if cached is not None and HeartbeatRequest is not None:
        try:
            await loop.run_in_executor(
                None, lambda: client.heartbeat(HeartbeatRequest(lease_id=cached))
            )
            return
        except Exception:
            # Expired / reaped / unknown lease_id — drop and re-acquire below.
            _lease_ids.pop(agent_uuid, None)

    if AcquireRequest is None:
        return
    result = await loop.run_in_executor(
        None,
        lambda: client.acquire(
            AcquireRequest(
                surface_id=f"agent:/{agent_uuid}",
                holder_agent_uuid=agent_uuid,
                holder_class="process_instance",
                holder_kind="remote_heartbeat",
                ttl_s=_PRESENCE_TTL_S,
                audit_session=client_session_id,
            )
        ),
    )
    # AcquireOk carries lease_id; failure variants (held_by_other, etc.) do not.
    new_id = getattr(result, "lease_id", None)
    if new_id:
        _lease_ids[agent_uuid] = str(new_id)
