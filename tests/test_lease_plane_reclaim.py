"""Tests for the #1460 lost-response recovery + own-orphan reclaim port.

Semantics under test are the #1459 council-reviewed design (see
``unitares_sdk/lease_plane/reclaim.py`` moduledoc):

* one idempotent same-body retry when an acquire response is lost;
* every holder uuid put on the wire is remembered per surface;
* a later held_by_other naming a remembered uuid → release the blocking
  lease with ``release_reason='reclaimed_lost_acquire'`` + same-call
  re-acquire with a fresh uuid;
* candidates leave memory only via absence-proof + grace — never age alone,
  never outcome-clearing;
* 'normal' fallback ONLY on 422 schema_invalid; transport/503 release
  failures keep the candidate and stay held_by_other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from src.lease_plane import (
    AcquireRequest,
    LeasePlaneClient,
    ReleaseRequest,
)
from src.lease_plane.advisory import acquire_advisory, release_advisory
from src.lease_plane.reclaim import ABSENCE_GRACE_S, MAX_CANDIDATES, ReclaimMemory

SURFACE = "dialectic:/test_reclaim_x"


def _ok_lease_payload(holder_uuid: UUID, surface_id: str = SURFACE) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "lease_id": str(uuid4()),
        "surface_id": surface_id,
        "surface_kind": "test",
        "holder_agent_uuid": str(holder_uuid),
        "holder_class": "process_instance",
        "holder_kind": "remote_heartbeat",
        "holder_pid": None,
        "heartbeat_required": True,
        "intent": "test",
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "released_at": None,
        "release_reason": None,
        "audit_session": None,
        "original_ttl_s": 60,
        "earned_status": "provisional",
    }


def _held_payload(held_by: UUID, blocking: UUID, surface_id: str = SURFACE) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "held_by_other",
        "surface_id": surface_id,
        "blocking_lease_id": str(blocking),
        "held_by_uuid": str(held_by),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        "retry_after_hint_ms": 0,
    }


class _Script:
    """Transport that raises or returns per scripted step and logs calls."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, req):
        self.calls.append((req.url.rsplit("/v1", 1)[-1], req.json_body))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _request(holder: UUID | None = None, surface_id: str = SURFACE) -> AcquireRequest:
    return AcquireRequest(
        surface_id=surface_id,
        holder_agent_uuid=holder or uuid4(),
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=60,
    )


# ---------------------------------------------------------------------------
# ReclaimMemory unit semantics
# ---------------------------------------------------------------------------


def test_memory_remembers_attempted_and_acquired():
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid="u-failed")
    mem.absorb(SURFACE, acquired_holder_uuid="u-acquired")
    assert mem.candidates(SURFACE) == frozenset({"u-failed", "u-acquired"})


def test_memory_never_expires_unproven_entries_by_age():
    # An orphan lives unboundedly — the stall-opening uuid must survive any
    # stall length. Only absence-proof starts the clock.
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid="u-old", now=0.0)
    mem.absorb(SURFACE, now=10 * ABSENCE_GRACE_S)  # far-future housekeeping pass
    assert "u-old" in mem.candidates(SURFACE)


def test_memory_drops_entry_only_after_absence_proof_plus_grace():
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid="u-lost", now=0.0)
    # A successful acquire proves no remembered uuid holds the surface NOW.
    mem.absorb(SURFACE, acquired_holder_uuid="u-winner", now=100.0)
    # Within grace the entry survives (delayed duplicate could still commit).
    mem.absorb(SURFACE, now=100.0 + ABSENCE_GRACE_S - 1)
    assert "u-lost" in mem.candidates(SURFACE)
    # Past grace it is gone.
    mem.absorb(SURFACE, now=100.0 + ABSENCE_GRACE_S + 1)
    assert "u-lost" not in mem.candidates(SURFACE)


def test_memory_new_acquire_uuid_is_exempt_from_its_own_stamp():
    mem = ReclaimMemory()
    mem.absorb(SURFACE, acquired_holder_uuid="u-winner", now=0.0)
    # The winner's own entry was appended unproven — a much later pass must
    # not have dropped it via the stamp it applied to OTHER entries.
    mem.absorb(SURFACE, now=ABSENCE_GRACE_S * 5)
    assert "u-winner" in mem.candidates(SURFACE)


def test_memory_is_per_surface():
    mem = ReclaimMemory()
    mem.absorb("resident:/a", attempted_holder_uuid="u-1")
    assert mem.candidates("resident:/b") == frozenset()


def test_memory_is_bounded():
    mem = ReclaimMemory()
    for i in range(MAX_CANDIDATES + 10):
        mem.absorb(SURFACE, attempted_holder_uuid=f"u-{i}")
    kept = mem.candidates(SURFACE)
    assert len(kept) == MAX_CANDIDATES
    assert f"u-{MAX_CANDIDATES + 9}" in kept  # newest kept
    assert "u-0" not in kept  # oldest dropped


# ---------------------------------------------------------------------------
# Lost-response retry (#1443 equivalent)
# ---------------------------------------------------------------------------


def test_transport_loss_retries_once_with_same_holder_uuid():
    holder = uuid4()
    script = _Script([
        ConnectionError("response lost"),
        {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": True},
    ])
    client = LeasePlaneClient(transport=script)

    outcome, lease_id = acquire_advisory(client, _request(holder), memory=ReclaimMemory())

    assert outcome == "acquired_idempotent"
    assert lease_id is not None
    assert len(script.calls) == 2
    assert script.calls[0][1]["holder_agent_uuid"] == str(holder)
    assert script.calls[1][1]["holder_agent_uuid"] == str(holder)  # SAME body


def test_recovered_retry_records_exactly_one_unproven_entry():
    # Regression (council review of this port): the lost-then-recovered path
    # must not absorb the uuid twice — the second (acquired) absorb would
    # stamp the first (attempted) entry absence-proven, marking the uuid's
    # OWN live lease as proven-absent and burning a second backstop slot.
    holder = uuid4()
    mem = ReclaimMemory()
    script = _Script([
        ConnectionError("response lost"),
        {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": True},
    ])
    acquire_advisory(LeasePlaneClient(transport=script), _request(holder), memory=mem)

    assert mem._by_surface[SURFACE] == [(str(holder), None)]


def test_default_module_store_is_used_and_resettable():
    # The production path shares one module-level store; reset_.. swaps it.
    from src.lease_plane import advisory as advisory_mod

    advisory_mod.reset_reclaim_memory_for_tests()
    holder = uuid4()
    script = _Script([ConnectionError("lost"), ConnectionError("lost again")])
    acquire_advisory(LeasePlaneClient(transport=script), _request(holder))
    assert str(holder) in advisory_mod._reclaim_memory.candidates(SURFACE)

    advisory_mod.reset_reclaim_memory_for_tests()
    assert advisory_mod._reclaim_memory.candidates(SURFACE) == frozenset()


def test_double_transport_loss_returns_service_unavailable_and_remembers():
    holder = uuid4()
    mem = ReclaimMemory()
    script = _Script([ConnectionError("lost"), ConnectionError("lost again")])
    client = LeasePlaneClient(transport=script)

    outcome, lease_id = acquire_advisory(client, _request(holder), memory=mem)

    assert outcome == "service_unavailable"
    assert lease_id is None
    assert str(holder) in mem.candidates(SURFACE)


def test_server_503_does_not_retry():
    # A received 503 is an answer, not a lost response — retrying would
    # double the load on a plane that is telling us it is unhealthy.
    script = _Script([{"ok": False, "error": "service_unavailable"}])
    client = LeasePlaneClient(transport=script)

    outcome, _ = acquire_advisory(client, _request(), memory=ReclaimMemory())

    assert outcome == "service_unavailable"
    assert len(script.calls) == 1


# ---------------------------------------------------------------------------
# Own-orphan reclaim
# ---------------------------------------------------------------------------


def test_conflict_naming_remembered_uuid_reclaims_and_reacquires():
    lost_holder = uuid4()
    blocking = uuid4()
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_holder))

    fresh_attempt_holder = uuid4()
    script = _Script([
        _held_payload(lost_holder, blocking),
        {"ok": True},  # release of the blocking lease
        {"ok": True, "lease": _ok_lease_payload(fresh_attempt_holder), "idempotent": False},
    ])
    client = LeasePlaneClient(transport=script)

    outcome, lease_id = acquire_advisory(client, _request(), memory=mem)

    assert outcome == "acquired_new"
    assert lease_id is not None
    paths = [p for p, _ in script.calls]
    assert paths == ["/lease/acquire", "/lease/release", "/lease/acquire"]
    release_body = script.calls[1][1]
    assert release_body["lease_id"] == str(blocking)
    assert release_body["release_reason"] == "reclaimed_lost_acquire"
    # Re-acquire uses a FRESH uuid — never the remembered one (the rejected
    # stable-holder-uuid design would make re-acquire re-entrant).
    assert script.calls[2][1]["holder_agent_uuid"] != str(lost_holder)
    assert script.calls[2][1]["holder_agent_uuid"] != script.calls[0][1]["holder_agent_uuid"]


def test_conflict_naming_unknown_uuid_is_left_alone():
    script = _Script([_held_payload(uuid4(), uuid4())])
    client = LeasePlaneClient(transport=script)

    outcome, lease_id = acquire_advisory(client, _request(), memory=ReclaimMemory())

    assert outcome == "held_by_other"
    assert lease_id is None
    assert len(script.calls) == 1  # no release attempted


def test_successful_acquire_uuid_is_reclaimable_after_lost_release():
    # The lost-RELEASE path: acquire ok, release response lost (nothing the
    # advisory can see), next run's acquire hits our own orphan.
    holder = uuid4()
    blocking = uuid4()
    mem = ReclaimMemory()

    ok_script = _Script([
        {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": False},
    ])
    acquire_advisory(LeasePlaneClient(transport=ok_script), _request(holder), memory=mem)

    retry_holder = uuid4()
    conflict_script = _Script([
        _held_payload(holder, blocking),
        {"ok": True},
        {"ok": True, "lease": _ok_lease_payload(retry_holder), "idempotent": False},
    ])
    outcome, lease_id = acquire_advisory(
        LeasePlaneClient(transport=conflict_script), _request(), memory=mem
    )

    assert outcome == "acquired_new"
    assert lease_id is not None
    assert conflict_script.calls[1][1]["release_reason"] == "reclaimed_lost_acquire"


def test_reclaim_release_not_found_proceeds_to_reacquire():
    lost_holder = uuid4()
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_holder))

    script = _Script([
        _held_payload(lost_holder, uuid4()),
        {"ok": False, "error": "not_found"},  # operator/reaper raced us — goal achieved
        {"ok": True, "lease": _ok_lease_payload(uuid4()), "idempotent": False},
    ])
    outcome, _ = acquire_advisory(LeasePlaneClient(transport=script), _request(), memory=mem)

    assert outcome == "acquired_new"


def test_reclaim_release_schema_invalid_falls_back_to_normal_once():
    # 422 is the ONE vintage signal: this plane's router predates the
    # reclaimed_lost_acquire reason. Fall back so reclaim works against an
    # old plane regardless of deploy order.
    lost_holder = uuid4()
    blocking = uuid4()
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_holder))

    script = _Script([
        _held_payload(lost_holder, blocking),
        {"ok": False, "error": "schema_invalid", "detail": "invalid release_reason"},
        {"ok": True},  # 'normal' fallback release
        {"ok": True, "lease": _ok_lease_payload(uuid4()), "idempotent": False},
    ])
    outcome, _ = acquire_advisory(LeasePlaneClient(transport=script), _request(), memory=mem)

    assert outcome == "acquired_new"
    assert script.calls[1][1]["release_reason"] == "reclaimed_lost_acquire"
    assert script.calls[2][1]["release_reason"] == "normal"
    assert script.calls[2][1]["lease_id"] == str(blocking)


def test_reclaim_release_transport_failure_keeps_candidate_and_conflict():
    # NO 'normal' fallback on transport/503: a timed-out release may still
    # have committed with the right reason, and a 503 may be an unapplied
    # 056 migration — fail loudly, keep memory, retry next attempt.
    lost_holder = uuid4()
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_holder))

    script = _Script([
        _held_payload(lost_holder, uuid4()),
        ConnectionError("release response lost"),
    ])
    outcome, lease_id = acquire_advisory(
        LeasePlaneClient(transport=script), _request(), memory=mem
    )

    assert outcome == "held_by_other"
    assert lease_id is None
    assert str(lost_holder) in mem.candidates(SURFACE)  # candidate preserved
    assert len(script.calls) == 2  # no 'normal' fallback, no re-acquire


def test_reclaim_does_not_recurse():
    # If the post-reclaim re-acquire ALSO hits a conflict naming a
    # remembered uuid, it must not reclaim again in the same call.
    lost_a, lost_b = uuid4(), uuid4()
    mem = ReclaimMemory()
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_a))
    mem.absorb(SURFACE, attempted_holder_uuid=str(lost_b))

    script = _Script([
        _held_payload(lost_a, uuid4()),
        {"ok": True},
        _held_payload(lost_b, uuid4()),  # nested attempt: conflict again
    ])
    outcome, _ = acquire_advisory(LeasePlaneClient(transport=script), _request(), memory=mem)

    assert outcome == "held_by_other"
    assert len(script.calls) == 3  # exactly one reclaim cycle, then stop


def test_reclaim_is_per_surface():
    lost_holder = uuid4()
    mem = ReclaimMemory()
    mem.absorb("dialectic:/test_reclaim_other", attempted_holder_uuid=str(lost_holder))

    script = _Script([_held_payload(lost_holder, uuid4())])
    outcome, _ = acquire_advisory(LeasePlaneClient(transport=script), _request(), memory=mem)

    # Same uuid but remembered for a DIFFERENT surface — no reclaim.
    assert outcome == "held_by_other"
    assert len(script.calls) == 1


# ---------------------------------------------------------------------------
# Release-side transport retry (flagged addition beyond the #1459 port)
# ---------------------------------------------------------------------------


def test_release_advisory_retries_once_on_transport_loss():
    script = _Script([ConnectionError("lost"), {"ok": True}])
    client = LeasePlaneClient(transport=script)

    release_advisory(client, uuid4())

    assert [p for p, _ in script.calls] == ["/lease/release", "/lease/release"]
    assert script.calls[0][1] == script.calls[1][1]  # identical body


def test_release_advisory_does_not_retry_on_server_error():
    script = _Script([{"ok": False, "error": "not_found"}])
    client = LeasePlaneClient(transport=script)

    release_advisory(client, uuid4())

    assert len(script.calls) == 1


# ---------------------------------------------------------------------------
# Contract plumbing
# ---------------------------------------------------------------------------


def test_release_reason_literal_accepts_reclaimed_lost_acquire():
    req = ReleaseRequest(lease_id=uuid4(), release_reason="reclaimed_lost_acquire")
    assert req.release_reason == "reclaimed_lost_acquire"


def test_client_release_does_not_block_reclaimed_reason():
    # The §7.10 guard rejects only 'forced' (separate per-path token);
    # reclaimed_lost_acquire must flow through to the plane.
    script = _Script([{"ok": True}])
    client = LeasePlaneClient(transport=script)
    result = client.release(
        ReleaseRequest(lease_id=uuid4(), release_reason="reclaimed_lost_acquire")
    )
    assert result.ok is True
    assert len(script.calls) == 1
