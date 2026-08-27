"""Internal identity attestation for lease-plane mutations.

The lease plane owns coordination, but governance remains the authority for
agent identity.  This endpoint lets the BEAM service present an opaque proof
and a claimed holder UUID without copying the governance signing secret into
the lease-plane process.
"""

from __future__ import annotations

import os
import uuid

from starlette.responses import JSONResponse

from src.http_routes import access


async def http_verify_lease_holder(request):
    """POST /v1/lease-holder/verify — verify proof ownership of a UUID.

    The proof is currently a fresh ``continuity_token``.  The request uses the
    deliberately generic ``identity_proof`` field so a future audience-scoped
    or asymmetric attestation can replace it without changing the lease-plane
    header contract.  Credentials are verified transiently and are never
    returned, persisted, or logged.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    holder = body.get("holder_agent_uuid")
    proof = body.get("identity_proof")
    try:
        canonical_holder = str(uuid.UUID(str(holder)))
    except (TypeError, ValueError, AttributeError):
        return JSONResponse(
            {
                "ok": False,
                "verified": False,
                "error": "schema_invalid",
                "detail": "holder_agent_uuid must be a UUID",
            },
            status_code=422,
        )

    if not isinstance(proof, str) or not proof:
        return JSONResponse(
            {
                "ok": False,
                "verified": False,
                "error": "identity_proof_invalid",
                "reason": "identity proof is missing or empty",
            },
            status_code=403,
        )

    from src.mcp_handlers.identity.session import recertify_strong_tier

    if not recertify_strong_tier(proof, canonical_holder):
        return JSONResponse(
            {
                "ok": False,
                "verified": False,
                "error": "identity_proof_invalid",
                "reason": "identity proof is invalid, expired, or bound to another identity",
            },
            status_code=403,
        )

    return JSONResponse(
        {
            "ok": True,
            "verified": True,
            "holder_agent_uuid": canonical_holder,
            "proof_type": "continuity_token.v1",
        }
    )
