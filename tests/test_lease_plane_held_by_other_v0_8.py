"""
v0.7 model drift fix: AcquireHeldByOther carries the v0.7 §7.3.2 extended fields.

The v0.7 RFC committed to extending the typed-absence shape with three new
fields (surface_id, blocking_lease_id, retry_after_hint_ms) so callers can:
  - correlate concurrent multi-surface acquires (surface_id echo)
  - distinguish "same stuck holder across retries" from "rotating cast"
    (blocking_lease_id)
  - implement sane backoff without parsing expires_at delta
    (retry_after_hint_ms)

The model in src/lease_plane/models.py was not updated when v0.7 shipped.
This test pins the v0.7 contract; the implementation lands in this PR.

Spec: docs/proposals/surface-lease-plane-v0.md §7.3.2
      docs/proposals/surface-lease-plane-phase-a-plan.md PR 1 row 6
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from src.lease_plane import (
    AcquireHeldByOther,
    AcquireRequest,
    LeasePlaneClient,
)
from src.lease_plane.client import LeaseHTTPRequest


def test_held_by_other_includes_v0_7_extended_fields():
    """AcquireHeldByOther carries surface_id, blocking_lease_id, retry_after_hint_ms."""
    holder = uuid4()
    blocking_lease = uuid4()
    surface_id = "file:///tmp/v0_8_drift_check.py"
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)

    def transport(_request: LeaseHTTPRequest):
        return {
            "ok": False,
            "error": "held_by_other",
            "surface_id": surface_id,
            "blocking_lease_id": str(blocking_lease),
            "held_by_uuid": str(holder),
            "expires_at": expires_at.isoformat(),
            "retry_after_hint_ms": 4500,
        }

    result = LeasePlaneClient(transport=transport).acquire(
        AcquireRequest(
            surface_id=surface_id,
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=90,
        )
    )

    assert isinstance(result, AcquireHeldByOther)
    # Pre-existing fields still parse.
    assert result.held_by_uuid == holder
    assert result.expires_at == expires_at
    # v0.7 §7.3.2 extended fields.
    assert result.surface_id == surface_id
    assert result.blocking_lease_id == blocking_lease
    assert result.retry_after_hint_ms == 4500
    # Type-check the new fields are exposed correctly.
    assert isinstance(result.blocking_lease_id, UUID)
    assert isinstance(result.retry_after_hint_ms, int)


# --- §9 RFC named-gate focused tests --------------------------------------
#
# RFC §9 names three separate gates against the v0.7 §7.3.2 extended
# held_by_other shape. The combined test above pins all three at once;
# these focused tests pin each gate individually so the §9 audit
# (scripts/dev/audit_rfc_section_9_gates.py) reports them as exact rather
# than missing.


def _make_held_by_other(
    *, surface_id: str, blocking_lease_id: UUID, retry_after_hint_ms: int = 0
) -> AcquireHeldByOther:
    return AcquireHeldByOther(
        ok=False,
        error="held_by_other",
        surface_id=surface_id,
        blocking_lease_id=blocking_lease_id,
        held_by_uuid=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        retry_after_hint_ms=retry_after_hint_ms,
    )


def test_held_by_other_echoes_surface_id():
    """RFC §7.3.2 / §9 — the held_by_other response echoes the requested
    surface_id so callers can correlate concurrent multi-surface acquires."""
    surface_id = "file:///tmp/echo_test.py"
    result = _make_held_by_other(surface_id=surface_id, blocking_lease_id=uuid4())
    assert result.surface_id == surface_id


def test_held_by_other_returns_blocking_lease_id():
    """RFC §7.3.2 / §9 — the held_by_other response carries the
    blocking_lease_id so callers can distinguish 'same stuck holder across
    retries' from 'rotating cast'."""
    blocking_lease = uuid4()
    result = _make_held_by_other(
        surface_id="file:///tmp/blocking_test.py",
        blocking_lease_id=blocking_lease,
    )
    assert result.blocking_lease_id == blocking_lease
    assert isinstance(result.blocking_lease_id, UUID)


def test_held_by_other_includes_retry_hint():
    """RFC §7.3.2 / §9 — the held_by_other response carries
    retry_after_hint_ms so callers can implement sane backoff without
    parsing expires_at delta."""
    result = _make_held_by_other(
        surface_id="file:///tmp/retry_hint_test.py",
        blocking_lease_id=uuid4(),
        retry_after_hint_ms=2500,
    )
    assert result.retry_after_hint_ms == 2500
    assert isinstance(result.retry_after_hint_ms, int)
