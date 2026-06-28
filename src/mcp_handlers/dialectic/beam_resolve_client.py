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


async def beam_resolve(
    session_id: str,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    resolution: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Commit a resolution via BEAM.

    Returns the BEAM result dict on success (the session row is resolved + saga
    committed BEAM-side), or ``None`` to signal the caller to fall back to the
    Python path. Never raises.
    """
    if not beam_resolution_enabled():
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
