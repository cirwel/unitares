"""Client for routing the dialectic SYNTHESIS->RESOLVED commit to BEAM.

dialectic-on-BEAM Slice 1.2. When ``UNITARES_DIALECTIC_BEAM_RESOLUTION`` is on,
the Python convergence path hands the finished resolution payload to the BEAM
lease plane (`POST /v1/dialectic/resolve-with-effect`), which owns the cross-runtime saga
slot and is the sole writer of the terminal `core.dialectic_sessions` row.

Fail-safe by construction: this returns ``None`` — telling the caller to use the
Python persistence path — whenever the flag is off, the lease plane is not
configured, a required field is missing, or BEAM is unreachable / returns an
ordinary non-OK response.  A typed policy refusal is authoritative and raises
``BeamDialecticPolicyRefusal`` instead of falling through to a second writer.
The Python B-4 guards protect every ordinary fallback path.
"""

import asyncio
import os
from typing import Any, Dict, Optional

from src.dialectic_session_writes import (
    KIND_CREATE,
    KIND_PHASE,
    KIND_RESOLVE,
    KIND_REVIEWER,
    record_session_write,
)
from src.logging_utils import get_logger

logger = get_logger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# `DialecticSaga.resolve` answers a resolve that finds the row already
# resolved/failed with `{:ok, %{status: existing, saga_id: nil, origin:
# :already_terminal}}` (dialectic_saga.ex, the `{:error, {:session_terminal,
# _}}` branch of `do_resolve`). The router serialises it with Jason, which
# encodes the atom as this string: body `{"ok": true, "status": "<existing>",
# "saga_id": null, "origin": "already_terminal", ...}`.
ALREADY_TERMINAL_ORIGIN = "already_terminal"
_AUTHORSHIP_POLICY_REFUSALS = {
    "reviewer_recused",
    "invalid_review_authorship",
    "session_identity_mismatch",
}


class BeamDialecticPolicyRefusal(RuntimeError):
    """BEAM authoritatively refused a dialectic write on governance policy."""

    def __init__(self, error_code: str, *, operation: str):
        self.error_code = error_code
        self.operation = operation
        super().__init__(f"{operation} refused by BEAM policy: {error_code}")


def _raise_authorship_policy_refusal(
    status: int, body: Any, *, operation: str
) -> None:
    error_code = body.get("error") if isinstance(body, dict) else None
    if status == 409 and error_code in _AUTHORSHIP_POLICY_REFUSALS:
        raise BeamDialecticPolicyRefusal(error_code, operation=operation)


async def _already_terminal_reason(session_id: str) -> Dict[str, Any]:
    """Read the terminal row's ``resolution_json->>'reason'``, best-effort.

    BEAM wrote no saga row for an already-terminal answer, so this reason is
    the only durable hint of who got there first (Wave 3 gate council
    2026-09-27, B2). ``terminal_reason_read=False`` means the read failed and a
    None reason says nothing; ``True`` with None is consistent with a sweeper
    reap, which writes no ``resolution_json``.
    """
    try:
        from src.dialectic_db import get_session_terminal_state_async

        row = await get_session_terminal_state_async(session_id)
        return {"terminal_reason": (row or {}).get("reason"), "terminal_reason_read": True}
    except Exception as e:
        logger.debug(
            f"[BEAM_RESOLVE] already_terminal reason read failed for {session_id[:16]}: {e}"
        )
        return {"terminal_reason": None, "terminal_reason_read": False}


def beam_resolution_enabled() -> bool:
    """True iff the operator has flipped UNITARES_DIALECTIC_BEAM_RESOLUTION on."""
    return os.getenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "0").strip().lower() in _TRUTHY


async def beam_create_session(
    session_id: str,
    paused_agent_id: Optional[str],
    fields: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a dialectic session via BEAM (it owns the write + starts a liveness
    watcher at birth). `fields` carries the optional columns (reviewer_agent_id,
    reason, dispute_type, session_type, topic, max_synthesis_rounds,
    synthesis_round, paused_agent_state, trigger_source, discovery_id).

    Returns the BEAM result on success, or ``None`` to fall back to the Python
    `create_session_async` path (flag off / no bearer / missing ids / BEAM
    unreachable / ordinary non-OK). Raises ``BeamDialecticPolicyRefusal`` for
    an authoritative authorship-policy refusal. Gated by the same
    UNITARES_DIALECTIC_BEAM_RESOLUTION flag as the resolve routing.
    """
    if not beam_resolution_enabled():
        return None

    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and paused_agent_id):
        return None

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
    payload = {"session_id": session_id, "paused_agent_id": paused_agent_id}
    for k, v in (fields or {}).items():
        if v is not None:
            payload[k] = v

    try:
        import httpx
    except Exception:  # pragma: no cover
        return None

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/session",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json=payload,
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_CREATE, session_id=session_id, default_via="beam", requested=None,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(f"[BEAM_CREATE] call failed for {session_id[:16]}, falling back to Python: {e}")
            rec.respond(outcome="no_response", error=type(e).__name__)
            return None

        if status in (200, 201) and isinstance(body, dict) and body.get("ok"):
            logger.info(f"[BEAM_CREATE] session {session_id[:16]} created on BEAM (created={body.get('created')})")
            # BEAM answers a duplicate id with ok=true, created=false: no write.
            rec.respond(outcome="not_written" if body.get("created") is False else "written",
                        http_status=status)
            return body

        rec.respond(outcome="not_written", http_status=status,
                    error=body.get("error") if isinstance(body, dict) else None)
        _raise_authorship_policy_refusal(status, body, operation="create_session")
        logger.warning(f"[BEAM_CREATE] non-OK ({status}) for {session_id[:16]}: {body}; falling back to Python")
        return None


def _phase_payload(
    session_id: str, phase: Optional[str], synthesis_round: Optional[int]
) -> Dict[str, Any]:
    """Phase-update body. Omits ``synthesis_round`` entirely when it is None so
    the wire shape is byte-identical to the pre-2026-08-16 request for callers
    that do not track rounds."""
    payload: Dict[str, Any] = {"session_id": session_id, "phase": phase}
    if synthesis_round is not None:
        payload["synthesis_round"] = int(synthesis_round)
    return payload


async def beam_update_phase(
    session_id: str,
    phase: Optional[str],
    synthesis_round: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Advance a non-terminal dialectic phase via BEAM (sole writer of the session
    row). Returns the BEAM result on success, or ``None`` to fall back to the
    Python `pg_update_phase` path. Never raises. Terminal phases are NOT routed
    here (those go via resolve); BEAM rejects them with 422 -> None -> fallback.

    ``synthesis_round`` is sent only when supplied. Deploy order is safe in both
    directions: the BEAM router pattern-matches on ``session_id``/``phase``, so a
    lease-plane that predates this field ignores the extra key and updates phase
    exactly as before; a lease-plane that has it treats an absent key as "leave
    the stored round alone".
    """
    if not beam_resolution_enabled():
        return None
    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and phase):
        return None

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")

    try:
        import httpx
    except Exception:  # pragma: no cover
        return None

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/phase",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json=_phase_payload(session_id, phase, synthesis_round),
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_PHASE, session_id=session_id, default_via="beam", requested=phase,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(f"[BEAM_PHASE] call failed for {session_id[:16]}, falling back: {e}")
            rec.respond(outcome="no_response", error=type(e).__name__)
            return None

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            # BEAM's update_phase answers :ok for a row that is already
            # terminal as well as for one it updated (zero rows written), so an
            # OK here does not prove the phase changed: its effect is unknown.
            rec.respond(outcome="accepted_effect_unknown", http_status=status)
            return body
        logger.warning(f"[BEAM_PHASE] non-OK ({status}) for {session_id[:16]}: {body}; falling back")
        rec.respond(outcome="not_written", http_status=status,
                    error=body.get("error") if isinstance(body, dict) else None)
        return None


async def beam_update_reviewer(
    session_id: str,
    reviewer_agent_id: Optional[str],
    *,
    claim_if_unassigned: bool = False,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Assign/reassign a reviewer via BEAM (sole writer of reviewer_agent_id).
    Returns the BEAM result on success, or ``None`` to fall back to the Python
    `pg_update_reviewer` path. Raises ``BeamDialecticPolicyRefusal`` for an
    authoritative authorship-policy refusal. ``detail["commit_unknown"]``
    distinguishes a response failure that may have followed the reviewer CAS
    from a definitive non-write, so a first responder can use exact readback
    instead of mistaking its own committed claim for a competing reviewer.
    """
    if not beam_resolution_enabled():
        return None
    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and reviewer_agent_id):
        return None

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")

    try:
        import httpx
    except Exception:  # pragma: no cover
        return None

    post_started = False

    def _post():
        nonlocal post_started
        payload = {
            "session_id": session_id,
            "reviewer_agent_id": reviewer_agent_id,
        }
        if claim_if_unassigned:
            payload["claim_if_unassigned"] = True
        with httpx.Client(timeout=5.0) as client:
            post_started = True
            resp = client.post(
                f"{base_url}/v1/dialectic/reviewer",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json=payload,
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_REVIEWER, session_id=session_id, default_via="beam", requested=reviewer_agent_id,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(f"[BEAM_REVIEWER] call failed for {session_id[:16]}, falling back: {e}")
            rec.respond(outcome="no_response", error=type(e).__name__)
            if post_started and detail is not None:
                detail["commit_unknown"] = True
            return None

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            rec.respond(outcome="written", http_status=status)
            return body
        commit_unknown = status >= 500 or 200 <= status < 300
        rec.respond(outcome=("commit_unknown" if commit_unknown else "not_written"),
                    http_status=status,
                    error=body.get("error") if isinstance(body, dict) else None)
        _raise_authorship_policy_refusal(status, body, operation="update_reviewer")
        if commit_unknown and detail is not None:
            detail["commit_unknown"] = True
            detail["http_status"] = status
        logger.warning(f"[BEAM_REVIEWER] non-OK ({status}) for {session_id[:16]}: {body}; falling back")
        return None


async def beam_resolve(
    session_id: str,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    resolution: Dict[str, Any],
    status: str = "resolved",
    *,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Commit a terminal dialectic transition via BEAM.

    ``status`` is "resolved" or "failed" — BEAM is the sole writer of the
    session row for both. Returns the BEAM result dict on success (row written
    + saga committed BEAM-side), or ``None`` to signal the caller to fall back
    to the Python path. ``detail["commit_unknown"]`` distinguishes a response
    failure that may have followed the row commit from an early/definitive
    non-write. Governance-policy refusals still raise.
    """
    if not beam_resolution_enabled():
        return None
    if status not in ("resolved", "failed"):
        return None

    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    # The BEAM endpoint requires non-empty session/paused/reviewer; a session
    # with no assigned reviewer falls back to the Python path.
    if not (bearer and session_id and paused_agent_id and reviewer_agent_id):
        return None

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
    # `status` is rebound to the HTTP status code below; keep what was asked.
    requested_status = status
    payload = {
        "session_id": session_id,
        "paused_agent_id": paused_agent_id,
        "reviewer_agent_id": reviewer_agent_id,
        "resolution": resolution,
        "status": status,
    }

    try:
        import httpx
    except Exception:  # pragma: no cover - httpx always present in this deploy
        return None

    post_started = False

    def _post():
        nonlocal post_started
        with httpx.Client(timeout=5.0) as client:
            post_started = True
            resp = client.post(
                f"{base_url}/v1/dialectic/resolve-with-effect",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            body = resp.json() if resp.content else {}
            return resp.status_code, body

    # One attempt record before the request, one response record after it,
    # paired by attempt_id (src/dialectic_session_writes.py). Recorded only
    # when a request is actually made: the early returns above sent nothing.
    async with record_session_write(
        kind=KIND_RESOLVE, session_id=session_id, default_via="beam",
        requested=requested_status,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(f"[BEAM_RESOLVE] call failed for {session_id[:16]}, falling back to Python: {e}")
            rec.respond(outcome="no_response", error=type(e).__name__)
            if post_started and detail is not None:
                # Once Client.post() is entered, a timeout, response decode
                # failure, or transport teardown cannot prove the server did
                # not commit before the response was lost. Exact PostgreSQL
                # readback decides whether the caller must route reconciliation.
                detail["commit_unknown"] = True
            return None

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            logger.info(
                f"[BEAM_RESOLVE] session {session_id[:16]} resolved on BEAM "
                f"(origin={body.get('origin')})"
            )
            origin = body.get("origin")
            fields: Dict[str, Any] = {
                "outcome": "written", "http_status": status, "origin": origin,
                "reported_status": body.get("status"), "saga_id": body.get("saga_id"),
            }
            if origin == ALREADY_TERMINAL_ORIGIN:
                # Nothing was written: the row was already terminal.
                fields["outcome"] = "already_terminal"
                fields.update(await _already_terminal_reason(session_id))
            rec.respond(**fields)
            return body

        commit_unknown = status >= 500 or 200 <= status < 300
        rec.respond(outcome=("commit_unknown" if commit_unknown else "not_written"),
                    http_status=status,
                    error=body.get("error") if isinstance(body, dict) else None)
        _raise_authorship_policy_refusal(status, body, operation="resolve_session")
        if commit_unknown and detail is not None:
            # A 5xx can be returned after commit_session_row succeeded but saga
            # finalization failed. A malformed/negative 2xx is equally
            # ambiguous. 4xx policy/conflict responses are definitive.
            detail["commit_unknown"] = True
            detail["http_status"] = status
        # Every non-OK falls back to the Python path, whose B-4 guard prevents
        # an overwrite. The detail above keeps 5xx/malformed-2xx distinguishable
        # from definitive 4xx refusals for exact-readback reconciliation.
        logger.warning(
            f"[BEAM_RESOLVE] non-OK ({status}) for {session_id[:16]}: {body}; falling back to Python"
        )
        return None
