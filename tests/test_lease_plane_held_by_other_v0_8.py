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


# §9: test_held_by_other_echoes_surface_id
# §9: test_held_by_other_returns_blocking_lease_id
# §9: test_held_by_other_includes_retry_hint
def test_held_by_other_includes_v0_7_extended_fields():
    """AcquireHeldByOther carries surface_id, blocking_lease_id, retry_after_hint_ms.

    Satisfies the three §9 named gates above (echoes_surface_id /
    returns_blocking_lease_id / includes_retry_hint) — the RFC named them
    separately but the implementation covers all three in one assertion
    suite. Aliases let `audit_rfc_section_9_gates.py` see the coverage
    without renaming this descriptive test.
    """
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
