"""Lease-plane substrate-state emission for resident agents (RFC §7.13).

Used by `unitares_sdk.client.UnitaresClient.checkin()` to emit each resident's
post-checkin EISV onto `lease_plane.surface_leases.substrate_state`. Per RFC
§7.13.4 dual-run authority, the emission is observational-only until each
resident's individual canary completes — `audit.events` (gated by PR 8's
`AUDIT_EISV_SYNC_ENABLED_RESIDENTS` env var) remains authoritative until the
operator removes that resident's name from the env var.

Net-new write path; does NOT replace `process_agent_update`. Failures are
swallowed to keep the checkin contract — RFC §7.13.4 contract: lease-plane
failures MUST NOT fail the resident's checkin loop.

Design:
- `KNOWN_RESIDENT_NAMES` mirrors `src/grounding/class_indicator.py::
  KNOWN_RESIDENT_LABELS`. Hardcoded here because the SDK is a standalone
  package that should not import from `src/`. Drift between the two lists
  is caught by `tests/test_substrate_resident_names_match_class_indicator.py`.
- Emission is GATED on `name in KNOWN_RESIDENT_NAMES` — non-resident agents
  using the SDK don't emit (the migration-034 `substrate_state_only_on_resident_kind`
  CHECK would reject them at the DB anyway; gating client-side is friendlier).
- Lease handle (`_LeaseCache`) is per-client-instance (one per resident loop).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Mirror of src/grounding/class_indicator.py::KNOWN_RESIDENT_LABELS.
# Drift caught by tests/test_substrate_resident_names_match_class_indicator.py.
KNOWN_RESIDENT_NAMES: frozenset[str] = frozenset(
    {"Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler"}
)


class _LeaseCache:
    """Per-client cached lease handle for substrate emission.

    First emission triggers acquire (idempotent on (surface_id, holder_uuid)
    per Repo.acquire — restart re-uses the existing lease row). Subsequent
    emissions trigger renew. Renew failure clears cache so next emission
    re-acquires.
    """

    def __init__(self) -> None:
        self.lease_id: UUID | None = None

    def reset(self) -> None:
        self.lease_id = None


def _resolve_resident_name(raw_name: str) -> str | None:
    """Match `raw_name` (case-sensitive) against KNOWN_RESIDENT_NAMES.

    Residents are constructed with the SDK's `onboard(name=...)` and the
    name string lands in `core.identities`. The casing convention is
    capitalized labels (Vigil, Sentinel, etc.) per CLAUDE.md identity
    rules. Returns None for non-resident or unrecognized names.
    """
    if not raw_name:
        return None
    if raw_name in KNOWN_RESIDENT_NAMES:
        return raw_name
    return None


def _build_substrate_state(metrics: dict[str, Any]) -> dict[str, Any]:
    """Project a checkin response's metrics into the §7.13.1.2 shape.

    Pulls E/I/S/V from the metrics object (set by process_agent_update on
    the server side). Sensor status defaults to 'healthy' — we only reach
    this code path when checkin succeeded, so by the SDK's reckoning the
    resident's substrate observation is intact. Future iterations may
    inspect metrics for NaN / out-of-range / staleness and emit 'degraded'
    or 'failed' with a `reason` field per §7.13.1.2.
    """
    return {
        "E": float(metrics.get("E", 0.0)),
        "I": float(metrics.get("I", 0.0)),
        "S": float(metrics.get("S", 0.0)),
        "V": float(metrics.get("V", 0.0)),
        "sensor": {"status": "healthy"},
    }


def _make_client():
    """Build a lease-plane client lazily.

    Imported lazily to keep module load light. The lease-plane client is
    part of this SDK (``unitares_sdk.lease_plane``); emission is skipped
    silently when no bearer token is configured.
    """
    from unitares_sdk.lease_plane.client import (
        LeasePlaneClient,
        LeasePlaneClientConfig,
        LeasePlaneDisabledClient,
    )

    token = os.environ.get("LEASE_PLANE_BEARER_TOKEN", "").strip()
    base_url = os.environ.get(
        "LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788"
    ).strip()

    if not token:
        return LeasePlaneDisabledClient()

    return LeasePlaneClient(
        LeasePlaneClientConfig(
            base_url=base_url,
            bearer_token=token,
            timeout_s=2.0,
        )
    )


def emit_substrate_observation(
    *,
    resident_name: str,
    holder_uuid: str,
    metrics: dict[str, Any],
    cache: _LeaseCache,
    client=None,
) -> bool:
    """Emit one substrate observation for a resident agent.

    Returns True iff the lease-plane call succeeded. Caller MUST ignore the
    return value for checkin contract — this is observational-only per
    RFC §7.13.4. Skips silently if `resident_name` isn't a known resident
    (non-resident agents shouldn't write `resident:/` surfaces).
    """
    matched = _resolve_resident_name(resident_name)
    if matched is None:
        return False
    if not holder_uuid:
        return False
    if not metrics:
        return False

    if client is None:
        client = _make_client()
        if client is None:
            return False

    surface_id = f"resident:/{matched.lower()}"
    substrate_state = _build_substrate_state(metrics)
    observed_at = datetime.now(UTC)

    if cache.lease_id is None:
        from unitares_sdk.lease_plane.models import AcquireOk, AcquireRequest

        try:
            request = AcquireRequest(
                surface_id=surface_id,
                holder_agent_uuid=UUID(holder_uuid),
                holder_class="substrate_earned",
                holder_kind="remote_heartbeat",
                ttl_s=1000,  # §7.5 v0.9 measurement: p99 × 1.5 rounded
                intent=f"{matched} resident heartbeat (RFC §7.13)",
                substrate_state=substrate_state,
                substrate_state_observed_at=observed_at,
            )
            result = client.acquire(request)
        except Exception as exc:  # noqa: BLE001 — observational-only by contract
            logger.debug("[substrate] %s acquire raised: %r", matched, exc)
            return False

        if isinstance(result, AcquireOk):
            cache.lease_id = result.lease.lease_id
            logger.info(
                "[substrate] %s acquired lease_id=%s (idempotent=%s)",
                matched,
                cache.lease_id,
                result.idempotent,
            )
            return True

        logger.debug("[substrate] %s acquire non-OK: %r", matched, type(result).__name__)
        return False

    from unitares_sdk.lease_plane.models import RenewRequest, SimpleOk

    try:
        renew_request = RenewRequest(
            lease_id=cache.lease_id,
            substrate_state=substrate_state,
            substrate_state_observed_at=observed_at,
        )
        result = client.renew(renew_request)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[substrate] %s renew raised: %r", matched, exc)
        cache.reset()
        return False

    if isinstance(result, SimpleOk):
        return True

    logger.debug(
        "[substrate] %s renew non-OK (%r); resetting for re-acquire",
        matched,
        type(result).__name__,
    )
    cache.reset()
    return False
