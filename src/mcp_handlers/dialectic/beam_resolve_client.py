"""Client for routing the dialectic SYNTHESIS->RESOLVED commit to BEAM.

dialectic-on-BEAM Slice 1.2. When ``UNITARES_DIALECTIC_BEAM_RESOLUTION`` is on,
the Python convergence path hands the finished resolution payload to the BEAM
lease plane (`POST /v2/dialectic/resolve-with-effect`), which owns the cross-runtime saga
slot and is the sole writer of the terminal `core.dialectic_sessions` row.

In compatibility mode this returns ``None`` — telling the caller to use the
Python persistence path — whenever the flag is off, the lease plane is not
configured, a required field is missing, or BEAM is unreachable / returns an
ordinary non-OK response. When dialectic identity binding is enforced, those
same paths fail closed: falling through would bypass the only writer that can
verify request-bound identity. Typed policy, in-flight-saga, and
transcript-frontier refusals are always authoritative.
"""

import asyncio
import hashlib
import json
import os
import uuid
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
DIALECTIC_ATTESTATION_SCOPE = frozenset(
    {
        ("POST", "/v1/dialectic/session"),
        ("POST", "/v1/dialectic/phase"),
        ("POST", "/v1/dialectic/reviewer"),
        ("POST", "/v1/dialectic/reviewer/claim"),
        ("POST", "/v2/dialectic/resolve-with-effect"),
    }
)

# `DialecticSaga.resolve` answers a resolve that finds the row already
# resolved/failed with `{:ok, %{status: existing, saga_id: nil, origin:
# :already_terminal}}` (dialectic_saga.ex, the `{:error, {:session_terminal,
# _}}` branch of `do_resolve`). The router serialises it with Jason, which
# encodes the atom as this string: body `{"ok": true, "status": "<existing>",
# "saga_id": null, "origin": "already_terminal", ...}`.
ALREADY_TERMINAL_ORIGIN = "already_terminal"
_WRITE_ORIGINS = {"new", "idempotent"}
_TERMINAL_STATUSES = {"resolved", "failed"}
_GOVERNANCE_POLICY_REFUSALS = {
    "self_review_not_authorizing",
    "reviewer_recused",
    "invalid_review_authorship",
    "session_identity_mismatch",
    "permission_denied",
}


class BeamDialecticPolicyRefusal(RuntimeError):
    """BEAM authoritatively refused a dialectic write on governance policy."""

    def __init__(self, error_code: str, *, operation: str):
        self.error_code = error_code
        self.operation = operation
        super().__init__(f"{operation} refused by BEAM policy: {error_code}")


class BeamDialecticSagaInFlight(RuntimeError):
    """A different terminal payload owns BEAM's durable saga slot."""


class BeamDialecticTranscriptConflict(RuntimeError):
    """BEAM refused a terminal write from stale protocol state."""


def _raise_governance_policy_refusal(status: int, body: Any, *, operation: str) -> None:
    error_code = body.get("error") if isinstance(body, dict) else None
    identity_reason = body.get("reason") if isinstance(body, dict) else None
    if (status == 409 and error_code in _GOVERNANCE_POLICY_REFUSALS) or (
        status in (403, 503)
        and error_code in {"permission_denied", "service_unavailable"}
        and identity_reason
        in {
            "identity_proof_invalid",
            "identity_proof_replayed",
            "identity_verification_unavailable",
        }
    ):
        error_code = identity_reason or error_code
        raise BeamDialecticPolicyRefusal(error_code, operation=operation)


def _valid_resolve_ack(body: Dict[str, Any], requested_status: str) -> bool:
    """Whether a positive BEAM response proves the claimed terminal outcome."""
    if body.get("ok") is not True:
        return False
    origin = body.get("origin")
    reported_status = body.get("status")
    saga_id = body.get("saga_id")
    if origin in _WRITE_ORIGINS:
        return (
            reported_status == requested_status
            and isinstance(saga_id, str)
            and bool(saga_id.strip())
        )
    if origin == ALREADY_TERMINAL_ORIGIN:
        return reported_status in _TERMINAL_STATUSES and saga_id is None
    return False


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
        return {
            "terminal_reason": (row or {}).get("reason"),
            "terminal_reason_read": True,
        }
    except Exception as e:
        logger.debug(
            f"[BEAM_RESOLVE] already_terminal reason read failed for {session_id[:16]}: {e}"
        )
        return {"terminal_reason": None, "terminal_reason_read": False}


def beam_resolution_enabled() -> bool:
    """True iff the operator has flipped UNITARES_DIALECTIC_BEAM_RESOLUTION on."""
    return (
        os.getenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "0").strip().lower() in _TRUTHY
    )


def dialectic_identity_enforced() -> bool:
    """Mirror the lease plane's effective enforcement mode for dialectic writes."""
    if os.getenv("UNITARES_LEASE_IDENTITY_BINDING", "off").strip().lower() != "enforce":
        return False
    raw_kinds = os.getenv("UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS")
    if raw_kinds is None or not raw_kinds.strip():
        return True
    if raw_kinds.strip().lower() == "all":
        return True
    kinds = {kind.strip() for kind in raw_kinds.split(",") if kind.strip()}
    return "dialectic" in kinds


def _fallback_or_refuse(operation: str) -> None:
    """Allow the legacy PostgreSQL writer only outside identity enforcement."""
    if dialectic_identity_enforced():
        raise BeamDialecticPolicyRefusal(
            "identity_verification_unavailable", operation=operation
        )
    return None


def _identity_bound_request(
    path: str,
    payload: Dict[str, Any],
    *,
    expected_agent_id: Optional[str] = None,
    include_actor: bool = False,
    fallback_actor_id: Optional[str] = None,
) -> Optional[tuple[bytes, Dict[str, str]]]:
    """Encode one BEAM mutation and bind it to the resolved MCP caller.

    The shared lease-plane bearer authenticates this deployment, not the agent
    whose dialectic row will change. A short-lived Ed25519 attestation binds the
    resolved caller to the exact path and bytes. Calls without a resolved
    request identity (for example a trusted background reconciliation task)
    still go through BEAM without a proof. Compatibility mode accepts that
    request; enforcement rejects it authoritatively instead of silently opening
    a second PostgreSQL writer.
    """
    from src.mcp_handlers.context import (
        get_context_resolved_agent_id,
        get_session_proof_origin,
    )
    from src.lease_attestation import LeaseAttestationError, mint_lease_attestation

    raw_actor = get_context_resolved_agent_id()
    actor: Optional[str] = None
    if raw_actor is not None:
        try:
            actor = str(uuid.UUID(str(raw_actor)))
            expected = (
                str(uuid.UUID(str(expected_agent_id))) if expected_agent_id else None
            )
        except (TypeError, ValueError, AttributeError):
            raise BeamDialecticPolicyRefusal(
                "identity_proof_invalid", operation=f"identity_binding:{path}"
            )
        if expected is not None and actor != expected:
            raise BeamDialecticPolicyRefusal(
                "identity_proof_invalid", operation=f"identity_binding:{path}"
            )
    elif expected_agent_id is not None:
        actor = str(expected_agent_id)
    elif fallback_actor_id is not None:
        actor = str(fallback_actor_id)

    wire_payload = dict(payload)
    if include_actor and actor is not None:
        wire_payload["actor_agent_id"] = actor
    body = json.dumps(
        wire_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {os.environ['LEASE_PLANE_BEARER_TOKEN']}",
        "Content-Type": "application/json",
    }
    if (
        raw_actor is not None
        and actor is not None
        and get_session_proof_origin() == "caller_asserted"
    ):
        try:
            headers["X-Unitares-Identity-Proof"] = mint_lease_attestation(
                holder_agent_uuid=actor,
                method="POST",
                path=path,
                body_sha256=hashlib.sha256(body).hexdigest(),
                request_scope=DIALECTIC_ATTESTATION_SCOPE,
            )
        except LeaseAttestationError as exc:
            logger.warning(
                "[BEAM_IDENTITY] attestation unavailable for %s; sending the "
                "request without proof so the lease plane applies its rollout mode: %s",
                path,
                exc,
            )
    return body, headers


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
    an authoritative governance-policy refusal. Gated by the same
    UNITARES_DIALECTIC_BEAM_RESOLUTION flag as the resolve routing.
    """
    if not beam_resolution_enabled():
        return _fallback_or_refuse("create_session")

    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and paused_agent_id):
        return _fallback_or_refuse("create_session")

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
    payload = {"session_id": session_id, "paused_agent_id": paused_agent_id}
    for k, v in (fields or {}).items():
        if v is not None:
            payload[k] = v

    bound_request = _identity_bound_request(
        "/v1/dialectic/session",
        payload,
        expected_agent_id=paused_agent_id,
    )
    if bound_request is None:
        return _fallback_or_refuse("create_session")
    body_bytes, headers = bound_request

    try:
        import httpx
    except Exception:  # pragma: no cover
        return _fallback_or_refuse("create_session")

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/session",
                headers=headers,
                content=body_bytes,
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_CREATE,
        session_id=session_id,
        default_via="beam",
        requested=None,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(
                f"[BEAM_CREATE] call failed for {session_id[:16]}, falling back to Python: {e}"
            )
            rec.respond(outcome="no_response", error=type(e).__name__)
            return _fallback_or_refuse("create_session")

        if status in (200, 201) and isinstance(body, dict) and body.get("ok"):
            logger.info(
                f"[BEAM_CREATE] session {session_id[:16]} created on BEAM (created={body.get('created')})"
            )
            # BEAM answers a duplicate id with ok=true, created=false: no write.
            rec.respond(
                outcome="not_written" if body.get("created") is False else "written",
                http_status=status,
            )
            return body

        rec.respond(
            outcome="not_written",
            http_status=status,
            error=body.get("error") if isinstance(body, dict) else None,
        )
        _raise_governance_policy_refusal(status, body, operation="create_session")
        logger.warning(
            f"[BEAM_CREATE] non-OK ({status}) for {session_id[:16]}: {body}; falling back to Python"
        )
        return _fallback_or_refuse("create_session")


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
    actor_agent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Advance a non-terminal dialectic phase via BEAM (sole writer of the session
    row). Returns the BEAM result on success, or ``None`` to fall back to the
    Python `pg_update_phase` path. Governance-policy refusals raise. Terminal phases are NOT routed
    here (those go via resolve); BEAM rejects them with 422 -> None -> fallback.

    ``synthesis_round`` is sent only when supplied. A BEAM version that predates
    that field ignores the extra key. Current BEAM also requires
    ``actor_agent_id``; an older Python caller therefore receives 422 and may
    use its compatibility fallback only while dialectic identity enforcement is
    off.
    """
    if not beam_resolution_enabled():
        return _fallback_or_refuse("update_phase")
    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and phase):
        return _fallback_or_refuse("update_phase")

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
    payload = _phase_payload(session_id, phase, synthesis_round)
    bound_request = _identity_bound_request(
        "/v1/dialectic/phase",
        payload,
        include_actor=True,
        fallback_actor_id=actor_agent_id,
    )
    if bound_request is None:
        return _fallback_or_refuse("update_phase")
    body_bytes, headers = bound_request

    try:
        import httpx
    except Exception:  # pragma: no cover
        return _fallback_or_refuse("update_phase")

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/phase",
                headers=headers,
                content=body_bytes,
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_PHASE,
        session_id=session_id,
        default_via="beam",
        requested=phase,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(
                f"[BEAM_PHASE] call failed for {session_id[:16]}, falling back: {e}"
            )
            rec.respond(outcome="no_response", error=type(e).__name__)
            return _fallback_or_refuse("update_phase")

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            # BEAM's update_phase answers :ok for a row that is already
            # terminal as well as for one it updated (zero rows written), so an
            # OK here does not prove the phase changed: its effect is unknown.
            rec.respond(outcome="accepted_effect_unknown", http_status=status)
            return body
        logger.warning(
            f"[BEAM_PHASE] non-OK ({status}) for {session_id[:16]}: {body}; falling back"
        )
        rec.respond(
            outcome="not_written",
            http_status=status,
            error=body.get("error") if isinstance(body, dict) else None,
        )
        _raise_governance_policy_refusal(status, body, operation="update_phase")
        return _fallback_or_refuse("update_phase")


async def beam_update_reviewer(
    session_id: str,
    reviewer_agent_id: Optional[str],
    *,
    claim_if_unassigned: bool = False,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Assign/reassign a reviewer via BEAM (sole writer of reviewer_agent_id).
    Returns the BEAM result on success, or ``None`` to fall back to the Python
    `pg_update_reviewer` path. Open-slot claims use a distinct route so an old
    BEAM returns 404 without executing its unconditional reviewer writer.
    Raises ``BeamDialecticPolicyRefusal`` for an authoritative authorship-policy
    refusal. ``detail["commit_unknown"]``
    distinguishes a response failure that may have followed the reviewer CAS
    from a definitive non-write, so a first responder can use exact readback
    instead of mistaking its own committed claim for a competing reviewer.
    """
    if not beam_resolution_enabled():
        return _fallback_or_refuse("update_reviewer")
    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    if not (bearer and session_id and reviewer_agent_id):
        return _fallback_or_refuse("update_reviewer")

    base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788").rstrip("/")
    route = (
        "/v1/dialectic/reviewer/claim"
        if claim_if_unassigned
        else "/v1/dialectic/reviewer"
    )
    payload = {
        "session_id": session_id,
        "reviewer_agent_id": reviewer_agent_id,
    }
    if claim_if_unassigned:
        payload["claim_if_unassigned"] = True
    bound_request = _identity_bound_request(
        route,
        payload,
        expected_agent_id=reviewer_agent_id if claim_if_unassigned else None,
        include_actor=True,
        fallback_actor_id=reviewer_agent_id,
    )
    if bound_request is None:
        return _fallback_or_refuse("update_reviewer")
    body_bytes, headers = bound_request

    try:
        import httpx
    except Exception:  # pragma: no cover
        return _fallback_or_refuse("update_reviewer")

    post_started = False

    def _post():
        nonlocal post_started
        with httpx.Client(timeout=5.0) as client:
            post_started = True
            resp = client.post(
                f"{base_url}{route}",
                headers=headers,
                content=body_bytes,
            )
            return resp.status_code, (resp.json() if resp.content else {})

    async with record_session_write(
        kind=KIND_REVIEWER,
        session_id=session_id,
        default_via="beam",
        requested=reviewer_agent_id,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(
                f"[BEAM_REVIEWER] call failed for {session_id[:16]}, falling back: {e}"
            )
            rec.respond(outcome="no_response", error=type(e).__name__)
            if post_started and detail is not None:
                detail["commit_unknown"] = True
                return None
            return _fallback_or_refuse("update_reviewer")

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            rec.respond(outcome="written", http_status=status)
            return body
        commit_unknown = status >= 500 or 200 <= status < 300
        rec.respond(
            outcome=("commit_unknown" if commit_unknown else "not_written"),
            http_status=status,
            error=body.get("error") if isinstance(body, dict) else None,
        )
        _raise_governance_policy_refusal(status, body, operation="update_reviewer")
        if commit_unknown and detail is not None:
            detail["commit_unknown"] = True
            detail["http_status"] = status
        logger.warning(
            f"[BEAM_REVIEWER] non-OK ({status}) for {session_id[:16]}: {body}; falling back"
        )
        if commit_unknown and detail is not None:
            return None
        return _fallback_or_refuse("update_reviewer")


async def beam_resolve(
    session_id: str,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    resolution: Dict[str, Any],
    status: str = "resolved",
    *,
    expected_phase: Optional[str] = None,
    expected_transcript_revision: Optional[int] = None,
    expected_awaiting_facilitation: Optional[bool] = False,
    expected_updated_at: Any = None,
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
        return _fallback_or_refuse("resolve_session")
    if status not in ("resolved", "failed"):
        return None
    # Only the distinct v2 route attests that BEAM understands the transcript
    # frontier. Calling the pre-frontier v1 route with an extra JSON key is not
    # safe during a rolling deploy: an old BEAM accepts and ignores that key,
    # then returns 200 for an unfenced terminal write. A caller without a
    # frontier uses the Python CAS path; old Python retains the v1 route for
    # rollback compatibility.
    if expected_transcript_revision is None:
        return _fallback_or_refuse("resolve_session")

    bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN") or ""
    # The BEAM endpoint requires non-empty session/paused/reviewer; a session
    # with no assigned reviewer falls back to the Python path.
    if not (bearer and session_id and paused_agent_id and reviewer_agent_id):
        return _fallback_or_refuse("resolve_session")

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
    if expected_transcript_revision is not None:
        payload["expected_transcript_revision"] = expected_transcript_revision
    if expected_phase is not None:
        payload["expected_phase"] = expected_phase
    if expected_awaiting_facilitation is not None:
        payload["expected_awaiting_facilitation"] = expected_awaiting_facilitation
    if expected_updated_at is not None:
        payload["expected_updated_at"] = (
            expected_updated_at.isoformat()
            if hasattr(expected_updated_at, "isoformat")
            else str(expected_updated_at)
        )
    bound_request = _identity_bound_request(
        "/v2/dialectic/resolve-with-effect",
        payload,
        include_actor=True,
        fallback_actor_id=paused_agent_id,
    )
    if bound_request is None:
        return _fallback_or_refuse("resolve_session")
    body_bytes, headers = bound_request

    try:
        import httpx
    except Exception:  # pragma: no cover - httpx always present in this deploy
        return _fallback_or_refuse("resolve_session")

    post_started = False

    def _post():
        nonlocal post_started
        with httpx.Client(timeout=5.0) as client:
            post_started = True
            resp = client.post(
                f"{base_url}/v2/dialectic/resolve-with-effect",
                headers=headers,
                content=body_bytes,
            )
            body = resp.json() if resp.content else {}
            return resp.status_code, body

    # One attempt record before the request, one response record after it,
    # paired by attempt_id (src/dialectic_session_writes.py). Recorded only
    # when a request is actually made: the early returns above sent nothing.
    async with record_session_write(
        kind=KIND_RESOLVE,
        session_id=session_id,
        default_via="beam",
        requested=requested_status,
    ) as rec:
        try:
            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, _post)
        except Exception as e:
            logger.warning(
                f"[BEAM_RESOLVE] call failed for {session_id[:16]}, falling back to Python: {e}"
            )
            rec.respond(outcome="no_response", error=type(e).__name__)
            if post_started and detail is not None:
                # Once Client.post() is entered, a timeout, response decode
                # failure, or transport teardown cannot prove the server did
                # not commit before the response was lost. Exact PostgreSQL
                # readback decides whether the caller must route reconciliation.
                detail["commit_unknown"] = True
                return None
            return _fallback_or_refuse("resolve_session")

        if (
            status == 200
            and isinstance(body, dict)
            and _valid_resolve_ack(body, requested_status)
        ):
            logger.info(
                f"[BEAM_RESOLVE] session {session_id[:16]} resolved on BEAM "
                f"(origin={body.get('origin')})"
            )
            origin = body.get("origin")
            fields: Dict[str, Any] = {
                "outcome": "written",
                "http_status": status,
                "origin": origin,
                "reported_status": body.get("status"),
                "saga_id": body.get("saga_id"),
            }
            if origin == ALREADY_TERMINAL_ORIGIN:
                # Nothing was written: the row was already terminal.
                fields["outcome"] = "already_terminal"
                fields.update(await _already_terminal_reason(session_id))
            rec.respond(**fields)
            return body

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            # A positive but malformed/mismatched response cannot prove which
            # transition, if any, committed. Never turn it into success or open
            # the Python terminal writer beneath it.
            rec.respond(
                outcome="commit_unknown",
                http_status=status,
                error="invalid_resolve_ack",
            )
            if detail is not None:
                detail["commit_unknown"] = True
                detail["http_status"] = status
                detail["invalid_ack"] = True
            logger.warning(
                f"[BEAM_RESOLVE] invalid positive acknowledgement for "
                f"{session_id[:16]}: {body}"
            )
            if detail is not None:
                return None
            return _fallback_or_refuse("resolve_session")

        commit_unknown = status >= 500 or 200 <= status < 300
        rec.respond(
            outcome=("commit_unknown" if commit_unknown else "not_written"),
            http_status=status,
            error=body.get("error") if isinstance(body, dict) else None,
        )
        error_code = body.get("error") if isinstance(body, dict) else None
        if status == 409 and error_code == "saga_in_flight":
            raise BeamDialecticSagaInFlight(
                "a different resolution saga is already in flight"
            )
        if status == 409 and error_code in {
            "session_phase_mismatch",
            "session_revision_mismatch",
            "session_facilitation_mismatch",
            "session_activity_mismatch",
            "saga_claim_lost",
        }:
            raise BeamDialecticTranscriptConflict(
                "the dialectic protocol state changed before terminal commit"
            )
        _raise_governance_policy_refusal(status, body, operation="resolve_session")
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
        if commit_unknown and detail is not None:
            return None
        return _fallback_or_refuse("resolve_session")
