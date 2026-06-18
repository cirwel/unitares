"""Orchestrator-vouched identity — INERT proof-of-concept seam.

⚠ THIS MODULE IS NOT WIRED INTO IDENTITY RESOLUTION. It is the pure-logic seam
for the design in ``docs/proposals/orchestrator-vouched-identity-v0.md`` (Wave 3,
deferred to the 2026-06-24 gate read). Nothing here is referenced by
``src/mcp_handlers/identity/resolution.py``, the tier classifier
(``identity_payloads.py``), or the strict write-gate (``phases.py``). Importing
it has no effect on live identity resolution.

The mechanism (full design in the RFC): the BEAM Agent Orchestrator — itself
S19-enrolled and speaking to governance over the kernel-peer-cred UDS channel —
VOUCHES for an ephemeral child it spawned by writing a short-TTL
``core.vouched_bindings`` row ``(child_uuid, child_os_pid, child_start_tvsec,
voucher_uuid, expires_at)``. When the child later connects over UDS, governance
reads the child's *own* kernel-attested peer PID + live start time and matches
them against the vouched row. On full match the child resolves at a genuine
``strong`` tier (``proof_origin = orchestrator_vouched``) — the first honest
strong cross-process credential for an ephemeral agent (the gap #807/#810 leaves
open), earned by attestation, not by echoing a copyable string.

This file deliberately mirrors the *shape* of ``src/substrate/verification.py``
(S19) but with the differences the council flagged:

- The lookup direction is **pid → row** (the child's agent_id is not known until
  the row is found), so there is NO ``agent_id``-keyed ``VerifiedPairsCache`` here;
  the anti-PID-reuse anchor is the ``start_tvsec`` stored *in the vouched row*
  (supplied by the voucher) compared against the child's *live* ``start_tvsec``
  at resolution (code-reviewer I2).
- ``start_tvsec`` is **mandatory-for-strong**: a binding or a live read missing
  it resolves to a rejection, never a weak-but-passing accept. Server-side TOFU
  on ``start_tvsec`` is rejected by design (RFC O3 / dialectic seam 2).

Boundary discipline (same as S19): pure logic only — no async, no DB, no
``set_session_proof_origin``, no env reads beyond the inert flag helper. The DB
table, the ``vouch_child`` handler, the BEAM HTTP-over-UDS client, and the
resolution-gate wiring are all cutover-row work, gated on the operator confirming
the #810 "by construction" reinterpretation (RFC §1 ⚠ box).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Reuse the S19 result shape so the cutover gate can treat a vouch outcome and a
# substrate-claim outcome uniformly at the call site.
from src.substrate.verification import VerificationResult

# ---------------------------------------------------------------------------
# Inert flag + label constants (DEFINED, NOT WIRED).
#
# These names are the contract the cutover row will adopt. They are referenced
# only inside this module and its tests; adding them to _STRONG_IDENTITY_SOURCES
# or session.py's _CALLER_ASSERTED_SOURCES is explicitly OUT OF SCOPE for the PoC
# (code-reviewer C2 inertness invariant — doing so would create a live gating
# point with no vouch check behind it).
# ---------------------------------------------------------------------------

#: Master flag. Default off/inert. The cutover wiring (resolution gate, handler)
#: must no-op unless this is truthy.
VOUCH_FLAG_ENV = "UNITARES_ORCHESTRATOR_VOUCH"

#: proof_origin value a vouched binding earns. DISTINCT from any S19 value
#: (RFC O1 resolved): a dynamic per-spawn runtime vouch has a different trust
#: derivation + failure mode than a static launchd substrate_claim.
PROOF_ORIGIN_ORCHESTRATOR_VOUCHED = "orchestrator_vouched"

#: session_source the cutover row will add to _STRONG_IDENTITY_SOURCES. Kept
#: here (not there) so the PoC stays inert.
SESSION_SOURCE_BEAM_ORCHESTRATED = "beam_orchestrated_attestation"


def is_orchestrator_vouch_enabled() -> bool:
    """Return whether the (inert, default-off) vouch path is enabled.

    The cutover wiring gates on this; today nothing calls it in the live path.
    Accepts the usual truthy spellings to match the project's other flag readers.
    """
    return os.environ.get(VOUCH_FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VouchedBinding:
    """Snapshot of one ``core.vouched_bindings`` row.

    Written by the (future) ``vouch_child`` handler after the voucher's own S19
    peer-cred check passed, and read at child-resolution time. Frozen so it
    cannot be mutated mid-verify.

    ``child_start_tvsec`` is ``Optional`` only to model the *rejected* TOFU case
    explicitly (a row that somehow lacks it must fail, not silently pass) — the
    voucher is required to supply it (RFC O3).
    """

    child_uuid: str
    child_os_pid: int
    child_start_tvsec: Optional[int]
    voucher_uuid: str
    #: epoch seconds; the row is only valid while now < expires_at.
    expires_at_epoch: int


# ---------------------------------------------------------------------------
# Pure verification
# ---------------------------------------------------------------------------

#: failure_code vocabulary (mirrors verification.py's style):
#:   no_binding   — no vouched row for the connecting pid
#:   expired      — the vouched row's TTL has passed
#:   missing_start_tvsec — row or live read lacks start_tvsec (TOFU rejected)
#:   pid_mismatch — (defensive) row pid != connecting pid
#:   start_mismatch — live start_tvsec != vouched start_tvsec (PID reuse)
#:   uuid_mismatch — child's claimed uuid != the vouched row's uuid


def verify_vouched_binding(
    binding: Optional[VouchedBinding],
    *,
    peer_pid: int,
    live_start_tvsec: Optional[int],
    claimed_child_uuid: str,
    now_epoch: int,
) -> VerificationResult:
    """Decide accept/reject for a child resolving against a vouched binding.

    Inputs are all caller-supplied facts; this function does no I/O. The caller
    (cutover row) is responsible for: (a) reading the kernel-attested
    ``peer_pid`` from the UDS scope, (b) reading the child's ``live_start_tvsec``
    via ``peer_attestation.read_process_start_time(peer_pid)``, (c) looking up the
    ``vouched_bindings`` row *by pid* and constructing ``binding``, and (d) on
    accept, stamping ``proof_origin = PROOF_ORIGIN_ORCHESTRATOR_VOUCHED`` and
    ``session_source = SESSION_SOURCE_BEAM_ORCHESTRATED``.

    Check order is significant: existence → TTL → start-time presence (both
    sides) → pid → start match → uuid. A failure short-circuits.
    """
    if binding is None:
        return VerificationResult(
            accepted=False,
            reason=f"no vouched binding for pid {peer_pid}; child is not orchestrator-vouched",
            failure_code="no_binding",
        )

    if now_epoch >= binding.expires_at_epoch:
        return VerificationResult(
            accepted=False,
            reason=(
                f"vouched binding for {binding.child_uuid} expired at "
                f"{binding.expires_at_epoch} (now {now_epoch}); re-vouch required"
            ),
            failure_code="expired",
        )

    # start_tvsec is mandatory-for-strong on BOTH sides. A row without it means
    # the voucher tried (or was coerced into) the rejected TOFU path; a live read
    # without it means we cannot defeat PID reuse. Either way: reject, never a
    # weak-pass (RFC O3 / dialectic seam 2).
    if binding.child_start_tvsec is None:
        return VerificationResult(
            accepted=False,
            reason=(
                f"vouched binding for {binding.child_uuid} lacks child_start_tvsec; "
                "server-side TOFU on start time is rejected by design"
            ),
            failure_code="missing_start_tvsec",
        )
    if live_start_tvsec is None:
        return VerificationResult(
            accepted=False,
            reason=f"could not read live start_tvsec for connecting pid {peer_pid}",
            failure_code="missing_start_tvsec",
        )

    if binding.child_os_pid != peer_pid:
        # Defensive: the caller looked the row up BY pid, so this should not
        # happen — but if a row is fetched by another key in a future refactor,
        # never trust a pid that does not match the kernel-attested peer.
        return VerificationResult(
            accepted=False,
            reason=(
                f"vouched pid {binding.child_os_pid} != kernel-attested peer pid {peer_pid}"
            ),
            failure_code="pid_mismatch",
        )

    if binding.child_start_tvsec != live_start_tvsec:
        # Same pid, different start time → the pid was recycled to a different
        # process since the vouch. This is the anti-PID-reuse anchor.
        return VerificationResult(
            accepted=False,
            reason=(
                f"start_tvsec mismatch for pid {peer_pid}: vouched "
                f"{binding.child_start_tvsec}, live {live_start_tvsec} (PID reuse)"
            ),
            failure_code="start_mismatch",
        )

    if binding.child_uuid != claimed_child_uuid:
        return VerificationResult(
            accepted=False,
            reason=(
                f"claimed child uuid {claimed_child_uuid} != vouched uuid "
                f"{binding.child_uuid} for pid {peer_pid}"
            ),
            failure_code="uuid_mismatch",
        )

    return VerificationResult(
        accepted=True,
        reason=(
            f"orchestrator-vouched: uuid={binding.child_uuid} pid={peer_pid} "
            f"start_tvsec={live_start_tvsec} voucher={binding.voucher_uuid}"
        ),
    )


# ---------------------------------------------------------------------------
# Schema DDL — AUTHORED, NOT APPLIED, NOT IN A MIGRATION SLOT.
#
# Held as a reviewable constant rather than a db/postgres/migrations/NNN_*.sql
# file ON PURPOSE: an unapplied migration slot would show as drift in the LOCAL
# unitares_doctor schema_migrations check until the operator applied it, and this
# PoC is explicitly deferred past 2026-06-24. The cutover row promotes this DDL
# verbatim into the next real migration slot and applies it manually per the
# project's MANUAL-migration discipline.
# ---------------------------------------------------------------------------

VOUCHED_BINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS core.vouched_bindings (
    child_uuid          UUID        NOT NULL,
    child_os_pid        INTEGER     NOT NULL,
    child_start_tvsec   BIGINT      NOT NULL,   -- mandatory: no server-side TOFU
    voucher_uuid        UUID        NOT NULL,   -- the S19-enrolled orchestrator
    spawn_reason        TEXT        NOT NULL DEFAULT 'subagent',
    vouched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,   -- short TTL; re-vouch to extend
    PRIMARY KEY (child_uuid)
);
-- Resolution looks up BY the kernel-attested connecting pid, then validates
-- start_tvsec + uuid. Partial-unique on the live (pid, start_tvsec) pair guards
-- against two live vouched processes claiming the same pid within a TTL window.
CREATE UNIQUE INDEX IF NOT EXISTS vouched_bindings_pid_start_ux
    ON core.vouched_bindings (child_os_pid, child_start_tvsec);
"""
