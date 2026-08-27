"""Cross-runtime contract tests for request-bound lease attestations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.lease_attestation import (
    LeaseAttestationError,
    export_public_jwks,
    load_signing_key,
    mint_lease_attestation,
    verify_lease_attestation,
)


VECTOR = json.loads(
    (Path(__file__).parent / "vectors" / "lease_attestation.json").read_text()
)


def test_python_minter_reproduces_cross_language_vector() -> None:
    key = load_signing_key(VECTOR["seed"])
    audience = VECTOR["jwks"]["audience"]
    assert (
        export_public_jwks(signing_key=key, issuer=VECTOR["issuer"], audience=audience)
        == VECTOR["jwks"]
    )

    token = mint_lease_attestation(
        holder_agent_uuid=VECTOR["holder_agent_uuid"],
        method=VECTOR["method"],
        path=VECTOR["path"],
        body_sha256=VECTOR["body_sha256"],
        signing_key=key,
        issuer=VECTOR["issuer"],
        audience=audience,
        now=2_000_000_000,
        ttl_seconds=30,
        jti="vector-nonce-1",
    )
    assert token == VECTOR["token"]


def test_reference_verifier_binds_every_request_dimension() -> None:
    valid = verify_lease_attestation(
        VECTOR["token"],
        jwks=VECTOR["jwks"],
        holder_agent_uuid=VECTOR["holder_agent_uuid"],
        method=VECTOR["method"],
        path=VECTOR["path"],
        body_sha256=VECTOR["body_sha256"],
        audience=VECTOR["jwks"]["audience"],
        now=VECTOR["now"],
    )
    assert valid is not None
    assert valid.claims["jti"] == "vector-nonce-1"

    for override in (
        {"holder_agent_uuid": "22222222-2222-4222-8222-222222222222"},
        {"method": "DELETE"},
        {"path": "/v1/lease/release"},
        {"body_sha256": "0" * 64},
        {"now": 2_000_000_031},
    ):
        kwargs = {
            "holder_agent_uuid": VECTOR["holder_agent_uuid"],
            "method": VECTOR["method"],
            "path": VECTOR["path"],
            "body_sha256": VECTOR["body_sha256"],
            "audience": VECTOR["jwks"]["audience"],
            "now": VECTOR["now"],
            **override,
        }
        assert (
            verify_lease_attestation(VECTOR["token"], jwks=VECTOR["jwks"], **kwargs)
            is None
        )


def test_tamper_untrusted_issuer_and_wrong_audience_fail_closed() -> None:
    audience = VECTOR["jwks"]["audience"]
    assert (
        verify_lease_attestation(
            VECTOR["token"] + "x", jwks=VECTOR["jwks"], audience=audience
        )
        is None
    )
    untrusted = {**VECTOR["jwks"], "issuer": "operator-b"}
    assert (
        verify_lease_attestation(VECTOR["token"], jwks=untrusted, audience=audience)
        is None
    )
    assert (
        verify_lease_attestation(
            VECTOR["token"], jwks=VECTOR["jwks"], audience="other-lease-plane"
        )
        is None
    )


def test_expiry_boundary_is_strict() -> None:
    assert (
        verify_lease_attestation(
            VECTOR["token"],
            jwks=VECTOR["jwks"],
            audience=VECTOR["jwks"]["audience"],
            now=2_000_000_030,
        )
        is None
    )


def test_mint_requires_explicit_operator_configuration(monkeypatch) -> None:
    monkeypatch.delenv("UNITARES_LEASE_ATTESTATION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_ATTESTATION_ISSUER", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_ATTESTATION_AUDIENCE", raising=False)
    with pytest.raises(LeaseAttestationError, match="SIGNING_KEY"):
        mint_lease_attestation(
            holder_agent_uuid=VECTOR["holder_agent_uuid"],
            method=VECTOR["method"],
            path=VECTOR["path"],
            body_sha256=VECTOR["body_sha256"],
        )


def test_mint_requires_explicit_destination_audience(monkeypatch) -> None:
    monkeypatch.delenv("UNITARES_LEASE_ATTESTATION_AUDIENCE", raising=False)
    key = load_signing_key(VECTOR["seed"])
    with pytest.raises(LeaseAttestationError, match="ATTESTATION_AUDIENCE"):
        mint_lease_attestation(
            holder_agent_uuid=VECTOR["holder_agent_uuid"],
            method=VECTOR["method"],
            path=VECTOR["path"],
            body_sha256=VECTOR["body_sha256"],
            signing_key=key,
            issuer=VECTOR["issuer"],
        )
