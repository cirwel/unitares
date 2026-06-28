"""Client for routing the dialectic SYNTHESIS->RESOLVED commit to BEAM.

dialectic-on-BEAM Slice 1.2. When ``UNITARES_DIALECTIC_BEAM_RESOLUTION`` is on,
the Python convergence path hands the finished resolution payload to the BEAM
lease plane (`POST /v1/dialectic/resolve`), which owns the cross-runtime saga
slot and is the sole writer of the terminal `core.dialectic_sessions` row.

Fail-safe by construction: this returns ``None`` — telling the caller to use the
Python ``pg_resolve_session`` path — whenever the flag is off, the lease plane
is not configured, a required field is missing, or BEAM is unreachable / returns
non-OK. It never raises. A resolution therefore never hangs or is lost because
of BEAM; the Python B-4 guard protects the row in either path.
"""

import asyncio
import os
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


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
    unreachable / non-OK). Never raises. Gated by the same
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

    try:
        loop = asyncio.get_running_loop()
        status, body = await loop.run_in_executor(None, _post)
    except Exception as e:
        logger.warning(f"[BEAM_CREATE] call failed for {session_id[:16]}, falling back to Python: {e}")
        return None

    if status in (200, 201) and isinstance(body, dict) and body.get("ok"):
        logger.info(f"[BEAM_CREATE] session {session_id[:16]} created on BEAM (created={body.get('created')})")
        return body

    logger.warning(f"[BEAM_CREATE] non-OK ({status}) for {session_id[:16]}: {body}; falling back to Python")
    return None


async def beam_update_phase(session_id: str, phase: Optional[str]) -> Optional[Dict[str, Any]]:
    """Advance a non-terminal dialectic phase via BEAM (sole writer of the session
    row). Returns the BEAM result on success, or ``None`` to fall back to the
    Python `pg_update_phase` path. Never raises. Terminal phases are NOT routed
    here (those go via resolve); BEAM rejects them with 422 -> None -> fallback.
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
                json={"session_id": session_id, "phase": phase},
            )
            return resp.status_code, (resp.json() if resp.content else {})

    try:
        loop = asyncio.get_running_loop()
        status, body = await loop.run_in_executor(None, _post)
    except Exception as e:
        logger.warning(f"[BEAM_PHASE] call failed for {session_id[:16]}, falling back: {e}")
        return None

    if status == 200 and isinstance(body, dict) and body.get("ok"):
        return body
    logger.warning(f"[BEAM_PHASE] non-OK ({status}) for {session_id[:16]}: {body}; falling back")
    return None


async def beam_update_reviewer(session_id: str, reviewer_agent_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Assign/reassign a reviewer via BEAM (sole writer of reviewer_agent_id).
    Returns the BEAM result on success, or ``None`` to fall back to the Python
    `pg_update_reviewer` path. Never raises.
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

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/reviewer",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json={"session_id": session_id, "reviewer_agent_id": reviewer_agent_id},
            )
            return resp.status_code, (resp.json() if resp.content else {})

    try:
        loop = asyncio.get_running_loop()
        status, body = await loop.run_in_executor(None, _post)
    except Exception as e:
        logger.warning(f"[BEAM_REVIEWER] call failed for {session_id[:16]}, falling back: {e}")
        return None

    if status == 200 and isinstance(body, dict) and body.get("ok"):
        return body
    logger.warning(f"[BEAM_REVIEWER] non-OK ({status}) for {session_id[:16]}: {body}; falling back")
    return None


async def beam_resolve(
    session_id: str,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    resolution: Dict[str, Any],
    status: str = "resolved",
) -> Optional[Dict[str, Any]]:
    """Commit a terminal dialectic transition via BEAM.

    ``status`` is "resolved" or "failed" — BEAM is the sole writer of the
    session row for both. Returns the BEAM result dict on success (row written
    + saga committed BEAM-side), or ``None`` to signal the caller to fall back
    to the Python path. Never raises.
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

    def _post():
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{base_url}/v1/dialectic/resolve",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            body = resp.json() if resp.content else {}
            return resp.status_code, body

    try:
        loop = asyncio.get_running_loop()
        status, body = await loop.run_in_executor(None, _post)
    except Exception as e:
        logger.warning(f"[BEAM_RESOLVE] call failed for {session_id[:16]}, falling back to Python: {e}")
        return None

    if status == 200 and isinstance(body, dict) and body.get("ok"):
        logger.info(
            f"[BEAM_RESOLVE] session {session_id[:16]} resolved on BEAM "
            f"(origin={body.get('origin')})"
        )
        return body

    # 409 saga_in_flight / 404 / 503 / malformed -> fall back to the Python
    # path, whose B-4 guard still prevents an overwrite.
    logger.warning(
        f"[BEAM_RESOLVE] non-OK ({status}) for {session_id[:16]}: {body}; falling back to Python"
    )
    return None
