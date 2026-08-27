"""Request-bound, operator-signed authorization for lease mutations.

``lat.v1`` is deliberately separate from the descriptive Agent Identity
Credential (``aic.v2``).  A lease attestation is performative: governance
verifies a live continuity proof, then delegates exactly one short-lived HTTP
mutation to the named lease-plane audience.  The operator publishes only the
Ed25519 public key; the signing seed stays in governance.

Token shape::

    lat.v1.<payload-base64url>.<signature-base64url>

The signature covers the domain-separated ``lat.v1.<payload>`` bytes.  Request
claims bind the token to the method, path, and SHA-256 of the exact body bytes.
The lease plane additionally consumes ``(issuer, jti)`` once, so capture cannot
be replayed even within the validity window.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - minimal installs intentionally omit it
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    Ed25519PrivateKey = Ed25519PublicKey = None  # type: ignore[assignment,misc]
    _CRYPTO_AVAILABLE = False


LEASE_ATTESTATION_PREFIX = "lat.v1."
LEASE_ATTESTATION_VERSION = 1
LEASE_ATTESTATION_SIGNING_KEY_ENV = "UNITARES_LEASE_ATTESTATION_SIGNING_KEY"
LEASE_ATTESTATION_ISSUER_ENV = "UNITARES_LEASE_ATTESTATION_ISSUER"
LEASE_ATTESTATION_AUDIENCE_ENV = "UNITARES_LEASE_ATTESTATION_AUDIENCE"
DEFAULT_TTL_SECONDS = 30
CLOCK_SKEW_SECONDS = 5


class LeaseAttestationError(RuntimeError):
    """Mint-time configuration or input failure."""


@dataclass(frozen=True)
class VerifiedLeaseAttestation:
    claims: dict[str, Any]
    signing_input: bytes


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("base64url value must be text")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _require_crypto() -> None:
    if not _CRYPTO_AVAILABLE:
        raise LeaseAttestationError(
            "Ed25519 lease attestations require the cryptography package"
        )


def load_signing_key(seed_b64u: str | None = None) -> "Ed25519PrivateKey":
    """Load the operator's 32-byte Ed25519 seed; never generate implicitly."""
    _require_crypto()
    seed_text = seed_b64u or os.getenv(LEASE_ATTESTATION_SIGNING_KEY_ENV)
    if not seed_text:
        raise LeaseAttestationError(
            f"{LEASE_ATTESTATION_SIGNING_KEY_ENV} is required to mint attestations"
        )
    try:
        seed = _b64u_decode(seed_text)
        if len(seed) != 32:
            raise ValueError("seed must decode to exactly 32 bytes")
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:
        raise LeaseAttestationError(f"invalid lease-attestation signing seed: {exc}") from exc


def configured_issuer(issuer: str | None = None) -> str:
    value = issuer or os.getenv(LEASE_ATTESTATION_ISSUER_ENV)
    if not isinstance(value, str) or not value.strip():
        raise LeaseAttestationError(
            f"{LEASE_ATTESTATION_ISSUER_ENV} is required to mint attestations"
        )
    value = value.strip()
    if len(value) > 256 or any(ch.isspace() for ch in value):
        raise LeaseAttestationError("lease-attestation issuer is invalid")
    return value


def configured_audience(audience: str | None = None) -> str:
    """Return the exact lease-plane deployment this token may authorize."""
    value = audience or os.getenv(LEASE_ATTESTATION_AUDIENCE_ENV)
    if not isinstance(value, str) or not value.strip():
        raise LeaseAttestationError(
            f"{LEASE_ATTESTATION_AUDIENCE_ENV} is required to mint attestations"
        )
    value = value.strip()
    if len(value) > 256 or any(ch.isspace() for ch in value):
        raise LeaseAttestationError("lease-attestation audience is invalid")
    return value


def _public_bytes(public_key: "Ed25519PublicKey") -> bytes:
    return public_key.public_bytes_raw()


def key_id(public_key: "Ed25519PublicKey") -> str:
    return hashlib.sha256(_public_bytes(public_key)).hexdigest()[:16]


def export_public_jwks(
    *,
    signing_key: "Ed25519PrivateKey" | None = None,
    issuer: str | None = None,
    audience: str | None = None,
) -> dict[str, Any]:
    """Return the public operator key document consumed by peer lease planes."""
    key = signing_key or load_signing_key()
    canonical_issuer = configured_issuer(issuer)
    canonical_audience = configured_audience(audience)
    public_key = key.public_key()
    return {
        "issuer": canonical_issuer,
        "audience": canonical_audience,
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "use": "sig",
                "alg": "EdDSA",
                "kid": key_id(public_key),
                "x": _b64u_encode(_public_bytes(public_key)),
            }
        ],
    }


def validate_body_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise LeaseAttestationError("body_sha256 must be 64 lowercase hex characters")
    if any(ch not in "0123456789abcdef" for ch in value):
        raise LeaseAttestationError("body_sha256 must be 64 lowercase hex characters")
    return value


def validate_method(value: object) -> str:
    if not isinstance(value, str):
        raise LeaseAttestationError("method must be POST")
    method = value.strip().upper()
    if method != "POST":
        raise LeaseAttestationError("method must be POST")
    return method


def validate_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/v1/lease/")
        or not re.fullmatch(r"/v1/lease/[A-Za-z0-9_/-]+", value)
        or len(value) > 256
    ):
        raise LeaseAttestationError("path must be a /v1/lease/ mutation path")
    return value


def mint_lease_attestation(
    *,
    holder_agent_uuid: str,
    method: str,
    path: str,
    body_sha256: str,
    signing_key: "Ed25519PrivateKey" | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    jti: str | None = None,
) -> str:
    """Mint one short-lived, content-bound lease mutation authorization."""
    key = signing_key or load_signing_key()
    canonical_issuer = configured_issuer(issuer)
    canonical_audience = configured_audience(audience)
    try:
        holder = str(uuid.UUID(str(holder_agent_uuid)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LeaseAttestationError("holder_agent_uuid must be a UUID") from exc

    issued = int(time.time() if now is None else now)
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise LeaseAttestationError("ttl_seconds must be an integer") from exc
    if ttl < 1 or ttl > 60:
        raise LeaseAttestationError("ttl_seconds must be between 1 and 60")

    claims: dict[str, Any] = {
        "v": LEASE_ATTESTATION_VERSION,
        "typ": "lease-attestation",
        "alg": "EdDSA",
        "kid": key_id(key.public_key()),
        "iss": canonical_issuer,
        "sub": holder,
        "aud": canonical_audience,
        "mth": validate_method(method),
        "pth": validate_path(path),
        "bsha": validate_body_sha256(body_sha256),
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl,
        "jti": jti or _b64u_encode(secrets.token_bytes(18)),
    }
    if (
        not isinstance(claims["jti"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", claims["jti"])
    ):
        raise LeaseAttestationError("jti must be non-empty text no longer than 128 characters")

    payload_b64 = _b64u_encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signing_input = (LEASE_ATTESTATION_PREFIX + payload_b64).encode("ascii")
    return f"{signing_input.decode('ascii')}.{_b64u_encode(key.sign(signing_input))}"


def verify_lease_attestation(
    token: str,
    *,
    jwks: dict[str, Any],
    holder_agent_uuid: str | None = None,
    method: str | None = None,
    path: str | None = None,
    body_sha256: str | None = None,
    audience: str | None = None,
    now: int | None = None,
) -> VerifiedLeaseAttestation | None:
    """Reference verifier used by tests and non-BEAM integrations."""
    if not _CRYPTO_AVAILABLE or not isinstance(token, str) or not token.startswith(
        LEASE_ATTESTATION_PREFIX
    ):
        return None
    try:
        expected_audience = configured_audience(audience)
    except LeaseAttestationError:
        return None

    parts = token[len(LEASE_ATTESTATION_PREFIX) :].split(".")
    if len(parts) != 2:
        return None
    payload_b64, signature_b64 = parts
    signing_input = (LEASE_ATTESTATION_PREFIX + payload_b64).encode("ascii")
    try:
        claims = json.loads(_b64u_decode(payload_b64))
        signature = _b64u_decode(signature_b64)
        if not isinstance(claims, dict):
            return None
        expected_issuer = jwks.get("issuer")
        if (
            claims.get("iss") != expected_issuer
            or jwks.get("audience") != expected_audience
        ):
            return None
        jwk = next(
            item
            for item in jwks.get("keys", [])
            if isinstance(item, dict) and item.get("kid") == claims.get("kid")
        )
        if (
            jwk.get("kty") != "OKP"
            or jwk.get("crv") != "Ed25519"
            or jwk.get("alg") != "EdDSA"
            or jwk.get("use") != "sig"
        ):
            return None
        public_key = Ed25519PublicKey.from_public_bytes(_b64u_decode(jwk["x"]))
        public_key.verify(signature, signing_input)
    except (InvalidSignature, StopIteration, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    except Exception:
        return None

    ts = int(time.time() if now is None else now)
    iat = claims.get("iat")
    nbf = claims.get("nbf")
    exp = claims.get("exp")
    jti = claims.get("jti")
    if (
        claims.get("v") != LEASE_ATTESTATION_VERSION
        or claims.get("typ") != "lease-attestation"
        or claims.get("alg") != "EdDSA"
        or claims.get("aud") != expected_audience
        or not isinstance(iat, int)
        or not isinstance(nbf, int)
        or not isinstance(exp, int)
        or nbf != iat
        or exp <= iat
        or exp - iat > 60
        or iat > ts + CLOCK_SKEW_SECONDS
        or nbf > ts + CLOCK_SKEW_SECONDS
        or exp <= ts
        or not isinstance(jti, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", jti)
    ):
        return None
    expected = {
        "sub": holder_agent_uuid,
        "mth": method.upper() if isinstance(method, str) else None,
        "pth": path,
        "bsha": body_sha256,
    }
    if any(value is not None and claims.get(name) != value for name, value in expected.items()):
        return None
    return VerifiedLeaseAttestation(claims=claims, signing_input=signing_input)
