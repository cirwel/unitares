"""Agent Identity Credential (AIC) — prototype of a third-party-verifiable,
server-signed identity *attestation*.

Ontology grounding (docs/ontology/identity.md, docs/ontology/plan.md S19)
========================================================================
This is a DESCRIPTIVE-stance primitive (identity.md "Three stances"). It reports
standing the agent has *already* accrued and lets an outside party verify the
issuing server's claim — it does NOT manufacture continuity, and it confers NO
authority. Contrast the `continuity_token`, which identity.md lists under
"Performative" / "retire or repurpose" precisely because possession of it is
treated as a resume credential (the S19 copyable-bearer vector, plan.md
2026-04-25).

Two design rules fall directly out of that:

1. **An AIC is an attestation, not a bearer credential.** It is the server
   asserting "as of time T, UUID U held handle H, substrate_class C, and earned
   trust_tier Y over N observations." Copying it grants nothing: it carries no
   session proof (`sid`) and `resume_capable=false`. This is what makes it safe
   where `continuity_token` is not — it cannot be replayed to *act as* U.

2. **Server-verifiable / non-exportable signing (S19 B-strict).** The operator
   decision in plan.md (2026-04-25) was that strict attestation must be
   "server-verifiable or non-exportable ... not another copyable secret in a
   plist." So the server signs with an Ed25519 *private* key it never exports;
   any third party verifies offline with the *public* key (published JWKS-style).
   This is the asymmetric upgrade the symmetric HMAC `continuity_token`
   (issuer-verifiable-only) cannot provide.

What this prototype is NOT
--------------------------
- Not a resume/auth path. It does not touch identity resolution, the strict
  write gate, or `continuity_token`. Wiring an attestation into onboard/identity
  responses and a `/.well-known` JWKS endpoint is a separate, operator-gated step.
- Not a closure of the S19 agent->server resume-proof gap. That needs the
  enrollment / process pre-registration the plan discusses. This module is the
  *verifiable-attestation building block* B-strict's "enrollment certificate"
  idea would stand on, demonstrated in the server->world direction.

Token shape
-----------
``aic.v2.<payload_b64url>.<sig_b64url>`` — the ``aic.`` prefix and ``v2`` version
(``opv:2``) are deliberately disjoint from the continuity_token's ``v1.<...>``
shape so the two can never be confused or cross-verified. The signature covers
the domain-separated bytes ``aic.v2.<payload_b64url>`` (prefix included), so a
payload lifted into another envelope will not verify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Dict, Iterable, Optional

try:  # cryptography is in the `full` extra; guard so importing this module
    # without it fails loudly rather than at an arbitrary later call site.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only in minimal installs
    Ed25519PrivateKey = Ed25519PublicKey = None  # type: ignore
    InvalidSignature = Exception  # type: ignore
    _CRYPTO_AVAILABLE = False


AIC_VERSION = 2
AIC_PREFIX = "aic.v2."
_DEFAULT_TTL_SECONDS = 24 * 3600  # attestation of standing, not a session visa
_CLOCK_SKEW_TOLERANCE = 30
_SIGNING_KEY_ENV = "UNITARES_AIC_SIGNING_KEY"  # base64url of a 32-byte Ed25519 seed


class AICError(RuntimeError):
    """Raised for mint-time misconfiguration (missing crypto / bad key)."""


# ── base64url helpers (no padding, URL-safe) ───────────────────────────────

def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ── key management ─────────────────────────────────────────────────────────

def generate_signing_key_seed() -> str:
    """Mint a fresh Ed25519 seed (base64url of 32 bytes) for operator config.

    The operator stores this in ``UNITARES_AIC_SIGNING_KEY`` (or, in a real
    deployment, a non-exportable keystore/HSM). It is the *private* half — it
    NEVER leaves the server. Only the derived public key is published.
    """
    _require_crypto()
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    return _b64u_encode(seed)


def _require_crypto() -> None:
    if not _CRYPTO_AVAILABLE:
        raise AICError(
            "Ed25519 support requires the 'cryptography' package "
            "(install the 'full' extra: pip install -e '.[full]')."
        )


def load_signing_key(seed_b64u: Optional[str] = None) -> "Ed25519PrivateKey":
    """Load the server signing key from a seed, or from the env var.

    Raises AICError if neither is available — callers in a real server would
    treat a missing AIC key as "attestations disabled", not auto-generate one
    (an ephemeral key would silently invalidate every previously-issued AIC).
    """
    _require_crypto()
    if seed_b64u is None:
        import os

        seed_b64u = os.getenv(_SIGNING_KEY_ENV)
    if not seed_b64u:
        raise AICError(
            f"No AIC signing key. Set {_SIGNING_KEY_ENV} to a base64url Ed25519 "
            "seed (see generate_signing_key_seed())."
        )
    try:
        seed = _b64u_decode(seed_b64u)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:  # malformed seed
        raise AICError(f"Invalid AIC signing seed: {exc}") from exc


def _public_bytes(key: "Ed25519PublicKey") -> bytes:
    return key.public_bytes_raw()


def key_id(public_key: "Ed25519PublicKey") -> str:
    """Stable key id (``kid``): first 16 hex of SHA-256 over the public key."""
    return hashlib.sha256(_public_bytes(public_key)).hexdigest()[:16]


def export_public_jwks(signing_key: Optional["Ed25519PrivateKey"] = None) -> Dict[str, Any]:
    """Export the public verification key as a JWKS-style document.

    This is what a ``/.well-known/unitares-identity-jwks`` endpoint would serve
    so any third party can verify AICs offline without contacting the issuer.
    """
    _require_crypto()
    key = signing_key or load_signing_key()
    pub = key.public_key()
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "use": "sig",
                "alg": "EdDSA",
                "kid": key_id(pub),
                "x": _b64u_encode(_public_bytes(pub)),
            }
        ]
    }


def _public_key_from_jwk(jwk: Dict[str, Any]) -> "Ed25519PublicKey":
    _require_crypto()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise AICError("JWK is not an Ed25519 OKP key")
    return Ed25519PublicKey.from_public_bytes(_b64u_decode(jwk["x"]))


# ── mint ───────────────────────────────────────────────────────────────────

def mint_identity_attestation(
    *,
    uuid: str,
    structured_agent_id: Optional[str] = None,
    role_family: Optional[str] = None,
    substrate_class: Optional[str] = None,
    trust_tier: Optional[Any] = None,
    observation_count: Optional[int] = None,
    lineage_state: Optional[str] = None,
    signing_key: Optional["Ed25519PrivateKey"] = None,
    now: Optional[int] = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    jti: Optional[str] = None,
) -> str:
    """Mint a server-signed identity attestation for ``uuid``.

    The claim set mirrors the identity.md five-layer taxonomy so a verifier sees
    *which* continuity is being attested, not a flat "this is X":

    - ``uuid``                — registry / process-instance anchor
    - ``structured_agent_id`` — public handle (role layer; cosmetic, never proof)
    - ``role_family`` / ``substrate_class`` — role + substrate layers
    - ``trust_tier`` / ``observation_count`` — behavioral layer (accrued standing)
    - ``lineage_state``       — declared causal lineage (not claimed continuity)

    ``stance="descriptive"``, ``resume_capable=false`` and ``authorizes=[]`` are
    baked into the payload so the credential is self-describing about its own
    (lack of) authority. There is intentionally no ``sid``/session field.
    """
    _require_crypto()
    if not uuid:
        raise AICError("uuid is required to mint an attestation")
    key = signing_key or load_signing_key()
    issued = int(now if now is not None else time.time())
    ttl = max(60, int(ttl_seconds))
    payload: Dict[str, Any] = {
        "v": AIC_VERSION,
        "opv": 2,
        "typ": "aic",
        "alg": "EdDSA",
        "stance": "descriptive",
        "kid": key_id(key.public_key()),
        # identity claims (five-layer taxonomy)
        "uuid": str(uuid),
        "structured_agent_id": structured_agent_id,
        "role_family": role_family,
        "substrate_class": substrate_class,
        "trust_tier": trust_tier,
        "observation_count": observation_count,
        "lineage_state": lineage_state,
        # self-describing authority boundary — this is an attestation, not a
        # bearer credential (see module docstring + S19).
        "resume_capable": False,
        "authorizes": [],
        # validity window
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl,
        "jti": jti or _b64u_encode(hashlib.sha256(
            f"{uuid}:{issued}:{structured_agent_id}".encode()
        ).digest()[:12]),
    }
    payload_b64 = _b64u_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    signed_message = (AIC_PREFIX + payload_b64).encode("ascii")
    sig = key.sign(signed_message)
    return f"{AIC_PREFIX}{payload_b64}.{_b64u_encode(sig)}"


# ── verify ───────────────────────────────────────────────────────────────--

def verify_identity_attestation(
    token: str,
    *,
    jwks: Optional[Dict[str, Any]] = None,
    public_key: Optional["Ed25519PublicKey"] = None,
    now: Optional[int] = None,
    revoked_jti: Iterable[str] = (),
) -> Optional[Dict[str, Any]]:
    """Verify an AIC offline and return its claims, or ``None`` on any failure.

    Supply EITHER a ``jwks`` document (the published verification keys — the
    third-party path) OR a single ``public_key``. Verification is total:
    structure, ``aic.v2`` prefix, signature, validity window, and revocation are
    all checked. A ``continuity_token`` (``v1.<...>``) returns ``None`` — the two
    envelopes are deliberately non-interchangeable.

    Note: a valid return means "the issuer authentically attested these claims",
    NOT "the presenter is this agent". An AIC is evidence of standing, never an
    authorization to act — callers must not use it as a resume/auth proof.
    """
    if not isinstance(token, str) or not token.startswith(AIC_PREFIX):
        return None
    if not _CRYPTO_AVAILABLE:
        return None
    rest = token[len(AIC_PREFIX):]
    parts = rest.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        claims = json.loads(_b64u_decode(payload_b64))
        sig = _b64u_decode(sig_b64)
    except Exception:
        return None
    if not isinstance(claims, dict) or claims.get("typ") != "aic" or claims.get("v") != AIC_VERSION:
        return None

    # Resolve the verification key (by kid when a JWKS is given).
    verify_key = public_key
    if verify_key is None:
        if not jwks:
            return None
        kid = claims.get("kid")
        jwk = None
        for candidate in jwks.get("keys", []):
            if candidate.get("kid") == kid:
                jwk = candidate
                break
        if jwk is None:
            return None
        try:
            verify_key = _public_key_from_jwk(jwk)
        except Exception:
            return None

    signed_message = (AIC_PREFIX + payload_b64).encode("ascii")
    try:
        verify_key.verify(sig, signed_message)
    except InvalidSignature:
        return None
    except Exception:
        return None

    ts = int(now if now is not None else time.time())
    nbf = int(claims.get("nbf", 0))
    exp = int(claims.get("exp", 0))
    if ts + _CLOCK_SKEW_TOLERANCE < nbf:
        return None
    if exp + _CLOCK_SKEW_TOLERANCE < ts:
        return None
    if claims.get("jti") in set(revoked_jti):
        return None
    return claims


def is_resume_credential() -> bool:
    """An AIC is never a resume/auth credential. Always False.

    Provided as an explicit, greppable invariant so future call sites cannot
    quietly start treating an AIC like a `continuity_token`.
    """
    return False
