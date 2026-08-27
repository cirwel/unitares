"""Identity proof exchange and public keys for lease-plane mutations.

The lease plane owns coordination, but governance remains the authority for
agent identity.  The legacy verifier lets the BEAM service present an opaque
proof during rollout.  The attestation endpoint instead exchanges that proof
inside governance for a short-lived request-bound Ed25519 credential.  Peer
lease planes consume only the credential and the public operator key.
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


async def http_attest_lease_holder(request):
    """POST /v1/lease-holder/attest — mint one request-bound authorization.

    The continuity proof is verified and discarded in this process.  It is
    never included in the response, logs, or signed claim set.
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

    proof = body.get("identity_proof")
    if not isinstance(proof, str) or not proof:
        return _attestation_denied()

    from src.mcp_handlers.identity.session import (
        extract_token_agent_uuid_safe,
        recertify_strong_tier,
    )

    try:
        holder = extract_token_agent_uuid_safe(proof)
        certified = bool(holder and recertify_strong_tier(proof, holder))
    except Exception:
        holder = None
        certified = False
    if not certified or not holder:
        return _attestation_denied()

    claimed_holder = body.get("holder_agent_uuid")
    if claimed_holder is not None:
        try:
            if str(uuid.UUID(str(claimed_holder))) != holder:
                return _attestation_denied()
        except (TypeError, ValueError, AttributeError):
            return _attestation_denied()

    from src.lease_attestation import LeaseAttestationError, mint_lease_attestation

    try:
        attestation = mint_lease_attestation(
            holder_agent_uuid=holder,
            method=body.get("method"),
            path=body.get("path"),
            body_sha256=body.get("body_sha256"),
        )
    except LeaseAttestationError as exc:
        message = str(exc)
        if message.startswith(("method ", "path ", "body_sha256 ", "holder_agent_uuid ")):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "schema_invalid",
                    "detail": message,
                },
                status_code=422,
            )
        return JSONResponse(
            {
                "ok": False,
                "error": "attestation_unavailable",
                "reason": "operator signing is not configured",
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "ok": True,
            "holder_agent_uuid": holder,
            "attestation": attestation,
            "proof_type": "lease-attestation.v1",
        }
    )


async def http_lease_attestation_keys(_request):
    """GET /v1/lease-holder/keys — publish operator verification keys."""
    from src.lease_attestation import LeaseAttestationError, export_public_jwks

    try:
        body = export_public_jwks()
    except LeaseAttestationError:
        return JSONResponse(
            {
                "ok": False,
                "error": "attestation_unavailable",
                "reason": "operator signing is not configured",
            },
            status_code=503,
        )
    return JSONResponse(
        {"ok": True, **body},
        headers={"Cache-Control": "public, max-age=300"},
    )


def _attestation_denied() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "identity_proof_invalid",
            "reason": "identity proof is invalid, expired, or bound to another identity",
        },
        status_code=403,
    )
