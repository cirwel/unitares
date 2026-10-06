"""S19 substrate-claim verification.

Pure verification logic: given a substrate-claim (from ``core.substrate_claims``)
and the kernel-attested peer PID of a connecting resident, decide accept or
reject. Uses the four primitives from ``peer_attestation.py`` (label,
executable path, start time) and a thread-safe in-process cache for
PID-reuse detection.

This module contains NO async or DB code. The caller (PR3b: UDS handler
integration) is responsible for: (a) async-loading the substrate-claim row
from PostgreSQL, (b) constructing the ``SubstrateClaim`` dataclass, and
(c) wrapping the call in ``loop.run_in_executor`` per the anyio-deadlock
constraint (CLAUDE.md "Known Issue").

Adversary coverage (proposal v2 §Adversary models):
- A1 (Hermes case): defeated by ``read_peer_pid`` upstream + ``no_claim``
  rejection if the agent_uuid lacks a substrate-claim row.
- A2 naive (copy + connect from non-launchd process): defeated by the
  ``label_mismatch`` rejection.
- A2 escalated (binary-substitution + ``launchctl kickstart``): defeated
  by the ``exec_mismatch`` rejection. Residual deployment risk if the
  binary path is same-UID-writable (warned at enrollment time).
- Q3(e) (PID reuse race): defeated by the ``pid_reuse`` rejection — the
  cache pins ``(pid, start_tvsec)`` on first verified connect; a recycled
  PID has a different start_tvsec for the new process.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    pass


# =============================================================================
# Data shapes
# =============================================================================


@dataclass(frozen=True)
class SubstrateClaim:
    """Snapshot of one row from ``core.substrate_claims``.

    Constructed by the DB-loading caller (PR3b) and passed to
    ``verify_substrate_claim``. Frozen so it can't be mutated mid-verify.
    """
    agent_id: str
    expected_launchd_label: str
    expected_executable_path: str
    enrolled_at: datetime
    enrolled_by_operator: bool
    notes: Optional[str] = None


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of a single verify call.

    ``accepted`` — True when the peer is the registered substrate.
    ``reason``   — human-readable explanation, useful in audit logs and
                   in the explicit-rejection message returned to the peer.
    ``failure_code`` — machine-readable failure category, ``None`` on accept.
                       One of: ``no_claim``, ``label_mismatch``,
                       ``exec_mismatch``, ``attestation_failed``,
                       ``pid_reuse``.
    """
    accepted: bool
    reason: str
    failure_code: Optional[str] = None


class _PeerAttestationLike(Protocol):
    """Subset of ``src.substrate.peer_attestation`` we depend on. Defined
    structurally so tests can inject a fake without subclassing."""
    @staticmethod
    def read_service_label(pid: int) -> Optional[str]: ...
    @staticmethod
    def read_executable_path(pid: int) -> Optional[str]: ...
    @staticmethod
    def read_process_start_time(pid: int) -> Optional[int]: ...


# =============================================================================
# VerifiedPairsCache
# =============================================================================


class VerifiedPairsCache:
    """In-process per-server-lifetime cache of ``agent_id → (pid, start_tvsec)``.

    Defeats Q3(e) PID-reuse: when a substrate-anchored agent first connects
    successfully, the cache pins its ``(pid, start_tvsec)``. A subsequent
    connect that lands on the same PID with a *different* start_tvsec —
    indicating PID reuse by an unrelated process — is rejected.

    A subsequent connect with a *different* PID (legitimate restart) is
    accepted and the cache entry is updated. Same PID + same start is the
    no-op happy path.

    Thread-safe; the lock is taken for atomic compare-and-update.
    """

    def __init__(self) -> None:
        self._pairs: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> Optional[tuple[int, int]]:
        """Return the cached ``(pid, start_tvsec)`` or ``None`` if uncached."""
        with self._lock:
            return self._pairs.get(agent_id)

    def record(self, agent_id: str, pid: int, start_tvsec: int) -> None:
        """Pin or update the cache entry for ``agent_id``."""
        with self._lock:
            self._pairs[agent_id] = (pid, start_tvsec)

    def clear(self) -> None:
        """Drop all cached pairs. Useful in tests; not used by production code."""
        with self._lock:
            self._pairs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pairs)


# =============================================================================
# verify_substrate_claim
# =============================================================================


def _get_peer_attestation_module() -> _PeerAttestationLike:
    """Default-import the peer_attestation module. Indirection allows tests
    to inject a fake module via the ``pa_module`` parameter."""
    from src.substrate import peer_attestation
    return peer_attestation  # type: ignore[return-value]


def verify_substrate_claim(
    claim: Optional[SubstrateClaim],
    peer_pid: int,
    *,
    pa_module: Optional[_PeerAttestationLike] = None,
    cache: Optional[VerifiedPairsCache] = None,
) -> VerificationResult:
    """Verify a substrate-claim against a kernel-attested peer PID.

    Returns a ``VerificationResult``; the caller (PR3b) translates the
    result into either an accept (continue with normal session binding)
    or an explicit rejection (return error pointing at the UDS path with
    the specific failure reason for audit).

    Order of checks is significant: cheaper checks first (label, executable),
    then the start-time check that maintains the cache. A check that fails
    short-circuits later checks; the cache is only updated on full success.
    """
    if claim is None:
        return VerificationResult(
            accepted=False,
            reason="no substrate-claim registered for this agent_uuid; "
                   "operator must run scripts/ops/enroll_resident.py first",
            failure_code="no_claim",
        )

    pa = pa_module if pa_module is not None else _get_peer_attestation_module()

    # ---- launchd label check ---------------------------------------------
    actual_label = pa.read_service_label(peer_pid)
    if actual_label != claim.expected_launchd_label:
        return VerificationResult(
            accepted=False,
            reason=(
                f"launchd label mismatch for PID {peer_pid}: "
                f"registered {claim.expected_launchd_label!r}, "
                f"observed {actual_label!r}"
            ),
            failure_code="label_mismatch",
        )

    # ---- executable path check -------------------------------------------
    actual_exec = pa.read_executable_path(peer_pid)
    if actual_exec != claim.expected_executable_path:
        return VerificationResult(
            accepted=False,
            reason=(
                f"executable path mismatch for PID {peer_pid}: "
                f"registered {claim.expected_executable_path!r}, "
                f"observed {actual_exec!r}"
            ),
            failure_code="exec_mismatch",
        )

    # ---- process start time + PID-reuse cache ---------------------------
    start_tvsec = pa.read_process_start_time(peer_pid)
    if start_tvsec is None:
        return VerificationResult(
            accepted=False,
            reason=f"could not read process start time for PID {peer_pid}",
            failure_code="attestation_failed",
        )

    if cache is not None:
        cached = cache.get(claim.agent_id)
        if cached is not None:
            cached_pid, cached_start = cached
            if cached_pid == peer_pid and cached_start != start_tvsec:
                # Same PID but different start_tvsec → PID was recycled
                # between the prior verified connect and this one.
                return VerificationResult(
                    accepted=False,
                    reason=(
                        f"PID reuse detected for {claim.agent_id}: "
                        f"PID {peer_pid} previously had start_tvsec={cached_start}, "
                        f"now {start_tvsec} (different process)"
                    ),
                    failure_code="pid_reuse",
                )
            # else: legitimate restart (different PID), or no-op match. Either
            # way the recorded value below is the now-current truth.
        cache.record(claim.agent_id, peer_pid, start_tvsec)

    return VerificationResult(
        accepted=True,
        reason=(
            f"substrate-claim verified: label={claim.expected_launchd_label} "
            f"pid={peer_pid} start_tvsec={start_tvsec}"
        ),
    )


# =============================================================================
# DB helper — fetch SubstrateClaim by agent_id
# =============================================================================


# UUIDs this process has seen with a substrate claim: every lookup that finds
# one adds it, a lookup that finds none drops it, and the server reloads the
# whole table every minute (load_known_substrate_claims, from
# background_tasks.substrate_claims_refresh), so an enrollment made by the
# operator script is known within a minute even if nothing looks it up. The
# HTTP-reject gates consult it only when a lookup fails, so an enrolled
# resident is still refused over HTTP while the claims table is unreachable
# (#2682). Until the table has loaded once, the set is incomplete, so every
# UUID counts as possibly claimed.
_known_claimed: set[str] = set()
_claims_loaded = False


def substrate_claims_loaded() -> bool:
    """Whether the claims table has loaded at least once in this process."""
    return _claims_loaded


def known_substrate_claimed(agent_id: Optional[str]) -> bool:
    """Whether ``agent_id`` may hold a substrate claim, for a gate whose own
    lookup just failed: last seen with one, or the table not yet loaded."""
    if not agent_id:
        return False
    return not _claims_loaded or str(agent_id) in _known_claimed


async def load_known_substrate_claims() -> int:
    """Replace the claimed set with the table's contents; returns how many.

    Raises if the read fails, leaving the previous set in place."""
    from src.db import get_db

    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT agent_id FROM core.substrate_claims")
    global _known_claimed, _claims_loaded
    _known_claimed = {str(r["agent_id"]) for r in rows}
    _claims_loaded = True
    return len(rows)


async def fetch_substrate_claim(agent_id: str) -> Optional[SubstrateClaim]:
    """Async lookup of one row from ``core.substrate_claims``.

    Returns ``None`` when no claim is registered for ``agent_id``. This
    helper is the "load" half of verify; the caller composes the two:

        claim = await fetch_substrate_claim(uuid)
        result = await loop.run_in_executor(None, verify_substrate_claim, claim, peer_pid)

    The split keeps the synchronous verify path (which calls launchctl /
    libproc) out of the anyio task group; only ``fetch_substrate_claim``
    awaits asyncpg.
    """
    from src.db import get_db

    db = get_db()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                agent_id,
                expected_launchd_label,
                expected_executable_path,
                enrolled_at,
                enrolled_by_operator,
                notes
            FROM core.substrate_claims
            WHERE agent_id = $1
            """,
            agent_id,
        )
    if row is None:
        _known_claimed.discard(str(agent_id))
        return None
    _known_claimed.add(str(agent_id))
    return SubstrateClaim(
        agent_id=row["agent_id"],
        expected_launchd_label=row["expected_launchd_label"],
        expected_executable_path=row["expected_executable_path"],
        enrolled_at=row["enrolled_at"],
        enrolled_by_operator=row["enrolled_by_operator"],
        notes=row["notes"],
    )
