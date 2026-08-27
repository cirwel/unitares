"""Proof tests for the lease plane's governance identity verifier."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from starlette.requests import Request

from src.http_routes.lease_identity import http_verify_lease_holder


HOLDER = "11111111-1111-4111-8111-111111111111"


def _request(payload: dict[str, str]) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/lease-holder/verify",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_matching_fresh_proof_is_attested_without_echoing_credential() -> None:
    proof = "v1.sensitive-payload.sensitive-signature"

    with (
        patch(
            "src.http_routes.lease_identity.access._check_http_auth", return_value=True
        ),
        patch(
            "src.mcp_handlers.identity.session.recertify_strong_tier",
            return_value=True,
        ) as verify,
    ):
        response = await http_verify_lease_holder(
            _request({"holder_agent_uuid": HOLDER, "identity_proof": proof})
        )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ok": True,
        "verified": True,
        "holder_agent_uuid": HOLDER,
        "proof_type": "continuity_token.v1",
    }
    verify.assert_called_once_with(proof, HOLDER)
    assert proof.encode() not in response.body


@pytest.mark.asyncio
async def test_mismatched_or_expired_proof_fails_closed_without_echoing_it() -> None:
    proof = "v1.sensitive-payload.sensitive-signature"

    with (
        patch(
            "src.http_routes.lease_identity.access._check_http_auth", return_value=True
        ),
        patch(
            "src.mcp_handlers.identity.session.recertify_strong_tier",
            return_value=False,
        ),
    ):
        response = await http_verify_lease_holder(
            _request({"holder_agent_uuid": HOLDER, "identity_proof": proof})
        )

    assert response.status_code == 403
    assert json.loads(response.body)["error"] == "identity_proof_invalid"
    assert proof.encode() not in response.body


@pytest.mark.asyncio
async def test_missing_proof_and_bad_uuid_are_typed_client_errors() -> None:
    with patch(
        "src.http_routes.lease_identity.access._check_http_auth", return_value=True
    ):
        missing = await http_verify_lease_holder(
            _request({"holder_agent_uuid": HOLDER})
        )
        malformed = await http_verify_lease_holder(
            _request({"holder_agent_uuid": "not-a-uuid", "identity_proof": "v1.x.y"})
        )

    assert missing.status_code == 403
    assert json.loads(missing.body)["error"] == "identity_proof_invalid"
    assert malformed.status_code == 422
    assert json.loads(malformed.body)["error"] == "schema_invalid"


@pytest.mark.asyncio
async def test_route_requires_governance_http_auth() -> None:
    with patch(
        "src.http_routes.lease_identity.access._check_http_auth", return_value=False
    ):
        response = await http_verify_lease_holder(
            _request(
                {
                    "holder_agent_uuid": str(uuid.uuid4()),
                    "identity_proof": "v1.x.y",
                }
            )
        )

    assert response.status_code == 401
