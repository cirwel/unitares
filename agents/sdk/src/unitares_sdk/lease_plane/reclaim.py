"""Per-surface memory of holder uuids this process has put on the wire.

Python port of the Elixir ``UnitaresSentinel.LeaseReclaim`` design (#1459,
council-reviewed — do not re-derive; issue #1460 is the port mandate), so a
later ``held_by_other`` that names one of our own uuids can be recognized as
this process's stranded lease and released.

## The gap this closes (2026-08-01 incident)

An acquire whose response is lost at the transport (server INSERT committed,
client's 2s budget expired) leaves the client without a ``lease_id``. The
lease plane auto-renews ``local_beam`` leases forever, so the lease is
immortal; every later attempt on that surface mints a fresh random uuid and
sees only ``held_by_other``. Enforced surfaces starve outright
(``resident:/ship_sh_claude/adjudication-evidence``, 2026-08-01); advisory
surfaces accrue unreapable orphans and permanently false held_by_other
telemetry. Yet every 409 names both the orphan's ``held_by_uuid`` (a uuid we
minted) and the ``blocking_lease_id`` needed to free it.

## What is remembered

Every holder uuid this process sends on a surface:

* an acquire whose transport attempts ALL failed contributes its holder uuid
  — that uuid MAY own a committed lease this process never learned about;
* a SUCCESSFUL acquire contributes its holder uuid too — if the eventual
  release request is lost, the plane auto-renews that lease forever, and the
  only way to recognize the resulting orphan is to still remember the uuid
  that acquired it (advisory release is best-effort, so one lost release is
  strictly MORE probable than the double-loss above).

## When an entry may be forgotten

Only after its lease is PROVEN absent, plus a grace window — never by age
alone, and never by outcome-based clearing:

* An orphan lives unboundedly (the plane-side holder auto-renews forever), so
  a uuid that might hold one must be remembered for as long as the starvation
  could persist. Pure age expiry would forget the stall-opening uuid during
  any stall longer than the window — the one uuid whose INSERT committed.
* A successful acquire proves that, at that instant, no remembered uuid holds
  this surface (one active lease per surface). It does NOT prove one cannot
  appear later: a delayed duplicate request can still commit afterwards. So a
  success stamps existing entries as absence-proven, and a stamped entry
  survives a further ``ABSENCE_GRACE_S`` before it is dropped.

The per-surface list is bounded (``MAX_CANDIDATES``) purely as a backstop.

In-memory only: a process restart forfeits reclaim for leases stranded before
the restart — for one-shot CLI callers (ship.sh) that residual is close to
total, and is carried by the doctor's ``immortal_lease`` check. Persisting
candidates across processes is deliberately NOT done here: a shared store
would let one live process release a concurrent sibling's lease, the exact
double-grant the reviewed design forbids.

## Safety

Holder uuids are minted process-locally (``advisory.new_holder_uuid`` →
``uuid.uuid4``). A match between ``held_by_uuid`` and a remembered uuid
therefore proves the blocking lease was created by an acquire THIS process
sent — releasing it can never take a lease away from another live holder.
(Fleet convention, not server-enforced: the plane does not authenticate
holder uuids. It holds because every client mints uuids randomly per attempt
and none echoes observed uuids back into acquire.) This is deliberately NOT
the rejected stable-holder-uuid design: uuids stay per-attempt, so two
concurrently live processes still contend correctly and can never adopt each
other's leases.
"""

from __future__ import annotations

import threading
import time

__all__ = ["ReclaimMemory"]

MAX_CANDIDATES = 4096
ABSENCE_GRACE_S = 15 * 60


class ReclaimMemory:
    """Thread-safe per-surface candidate store.

    Entries are ``(holder_uuid, absence_proven_at)`` — ``None`` until a
    successful acquire proves the uuid holds nothing on that surface.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_surface: dict[str, list[tuple[str, float | None]]] = {}

    def candidates(self, surface_id: str) -> frozenset[str]:
        """Uuids that may own a stranded lease on ``surface_id``."""
        with self._lock:
            return frozenset(u for u, _ in self._by_surface.get(surface_id, []))

    def absorb(
        self,
        surface_id: str,
        *,
        attempted_holder_uuid: str | None = None,
        acquired_holder_uuid: str | None = None,
        now: float | None = None,
    ) -> None:
        """Update reclaim memory from one acquire's outcome.

        Call after EVERY acquire attempt, whatever its outcome:

        1. drop entries whose absence was proven more than the grace window
           ago;
        2. on a successful acquire (``acquired_holder_uuid`` set), stamp
           still-unproven entries as absence-proven now — the new acquire's
           own uuid is appended fresh afterwards, so it is exempt (its lease
           is the one currently active);
        3. remember ``attempted_holder_uuid`` (every transport attempt
           failed) and ``acquired_holder_uuid`` (successful acquire) as
           fresh, unproven entries.

        ``now`` — injected clock (``time.monotonic`` domain) for tests.
        """
        ts = time.monotonic() if now is None else now
        acquired = acquired_holder_uuid is not None

        fresh = [
            (u, None)
            for u in (attempted_holder_uuid, acquired_holder_uuid)
            if isinstance(u, str) and u
        ]

        with self._lock:
            entries = self._by_surface.get(surface_id, [])

            survivors = [
                (u, proven)
                for u, proven in entries
                if proven is None or (ts - proven) <= ABSENCE_GRACE_S
            ]
            if acquired:
                survivors = [
                    (u, ts if proven is None else proven) for u, proven in survivors
                ]

            merged = (survivors + fresh)[-MAX_CANDIDATES:]
            if merged:
                self._by_surface[surface_id] = merged
            else:
                self._by_surface.pop(surface_id, None)
