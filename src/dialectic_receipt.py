"""Deployment-signed dialectic resolution receipts (``drr.v1``).

What this is
------------
A dialectic resolution carries two *symmetric* attestations: each party's
HMAC-SHA256 over the canonical payload, keyed on that party's ``api_key``
(``Resolution.compute_signature``). That construction is sound inside one
operator's trust boundary and is deliberately retained. It is also, by
construction, unverifiable by anyone who does not hold the keys, and a second
principal is exactly the party who cannot be handed them
(``docs/SCOPE_AND_THREAT_MODEL.md``, "The attestation half of the same
boundary").

A receipt is the deployment's own Ed25519 signature over the *stored record*:
the resolution dict as persisted, minus the receipt itself. It is signed with
the deployment's server-to-world attestation key, the same key and JWKS shape
as the Agent Identity Credential (``src/identity/agent_identity_credential.py``),
so a peer who has pinned this deployment's public key verifies it offline with
no ``api_key``, no database, and no server round-trip.

What a valid receipt proves, and what it does not
-------------------------------------------------
Proves: *this deployment* (the holder of the private half of ``kid``) issued
exactly this record — action, conditions, root cause, reasoning, timestamp,
both symmetric signatures and their version — for this session and these
parties, at ``iat``.

Does not prove: that either party *intended* the resolution. Party-level
non-repudiation would need party-held asymmetric keys, a separate decision
(shelved 2026-04-19). This is the "witness signs a receipt third parties
verify" construction the threat model names as one option among several; it
does not settle which verification semantics a multi-principal deployment
requires, it makes one of them available.

Posture
-------
Off by default. A receipt is minted only when ``UNITARES_AIC_SIGNING_KEY`` is
configured. With no key ``Resolution.receipt`` stays ``""`` and nothing else
changes. A configured-but-invalid key logs a warning and also yields ``""``:
finalizing a resolution is a liveness path for a paused agent and must not
fail on attestation plumbing. An absent receipt therefore means "the
deployment had no usable attestation key at finalization"; it says nothing
about the resolution.

Canonical form (for a verifier written in any language)
-------------------------------------------------------
``record_sha256`` is SHA-256 over the JSON encoding of the record with:

* the ``receipt`` key removed;
* ``conditions``, when it is a list, reduced to each item stringified and
  stripped of surrounding whitespace, empty items dropped (this is exactly the
  normalization the server's own read path applies, so the raw database row
  and the served response hash identically);
* keys sorted, separators ``,`` and ``:`` with no whitespace, non-ASCII
  escaped as ``\\uXXXX`` (Python ``json.dumps(sort_keys=True,
  separators=(",", ":"))``).

The signed message is the ASCII bytes of ``"drr.v1." + payload_b64url``; the
receipt is ``"drr.v1." + payload_b64url + "." + signature_b64url`` with
unpadded base64url throughout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Mapping, Optional

from src.identity.agent_identity_credential import (
    AICError,
    _b64u_decode,
    _b64u_encode,
    key_id,
    load_signing_key_if_configured,
)

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when cryptography is absent
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    Ed25519PrivateKey = Ed25519PublicKey = None  # type: ignore[assignment,misc]
    _CRYPTO_AVAILABLE = False

logger = logging.getLogger(__name__)

RECEIPT_PREFIX = "drr.v1."
RECEIPT_VERSION = 1
RECEIPT_TYP = "dialectic_resolution_receipt"
RECEIPT_FIELD = "receipt"


class ReceiptError(ValueError):
    """A receipt did not verify. ``code`` is a stable, greppable reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ── canonical form ─────────────────────────────────────────────────────────


def _canonical_conditions(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return value


def canonical_record_bytes(record: Mapping[str, Any]) -> bytes:
    """The exact bytes the receipt commits to. See the module docstring."""
    reduced: Dict[str, Any] = {k: v for k, v in dict(record).items() if k != RECEIPT_FIELD}
    if "conditions" in reduced:
        reduced["conditions"] = _canonical_conditions(reduced["conditions"])
    return json.dumps(reduced, sort_keys=True, separators=(",", ":")).encode("utf-8")


def record_sha256(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_record_bytes(record)).hexdigest()


# ── mint ───────────────────────────────────────────────────────────────────


def mint_resolution_receipt(
    record: Mapping[str, Any],
    *,
    session_id: str,
    paused_agent_id: str,
    reviewer_agent_id: Optional[str],
    signing_key: "Ed25519PrivateKey",
    now: Optional[int] = None,
) -> str:
    """Sign ``record`` with the deployment key and return the ``drr.v1`` receipt.

    The claim set is deliberately small and self-describing: ``authorizes`` is
    empty and ``stance`` is ``descriptive`` so a receipt can never be mistaken
    for a credential. ``bilateral_symmetric`` records whether both symmetric
    signatures were present at finalization (an LLM-assisted session leaves
    ``signature_b`` empty by design); it is the deployment reporting what it
    observed, not a claim about intent.
    """
    if not _CRYPTO_AVAILABLE:
        raise ReceiptError("crypto_unavailable", "Ed25519 receipts require the cryptography package")
    if not session_id:
        raise ReceiptError("missing_session_id", "a receipt must be bound to a session id")
    payload: Dict[str, Any] = {
        "v": RECEIPT_VERSION,
        "typ": RECEIPT_TYP,
        "alg": "EdDSA",
        "stance": "descriptive",
        "authorizes": [],
        "kid": key_id(signing_key.public_key()),
        "iat": int(now if now is not None else time.time()),
        "session_id": str(session_id),
        "paused_agent_id": str(paused_agent_id or ""),
        "reviewer_agent_id": str(reviewer_agent_id) if reviewer_agent_id else None,
        "record_sha256": record_sha256(record),
        "signature_version": record.get("signature_version"),
        "bilateral_symmetric": bool(record.get("signature_a")) and bool(record.get("signature_b")),
    }
    payload_b64 = _b64u_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = signing_key.sign((RECEIPT_PREFIX + payload_b64).encode("ascii"))
    return f"{RECEIPT_PREFIX}{payload_b64}.{_b64u_encode(signature)}"


def attach_receipt_if_configured(
    resolution: Any,
    *,
    session_id: str,
    paused_agent_id: str,
    reviewer_agent_id: Optional[str],
) -> str:
    """Mint and attach a receipt when the deployment has an attestation key.

    Returns the receipt, or ``""`` when no key is configured or minting could
    not proceed. Never raises: a resolution must persist even if attestation
    plumbing is broken, and the failure is logged at WARNING so it is not
    silent.
    """
    try:
        signing_key = load_signing_key_if_configured()
    except AICError as exc:
        logger.warning(
            "dialectic receipt: attestation key is configured but unusable (%s); "
            "resolution for session %s stored without receipt",
            exc, session_id,
        )
        return ""
    if signing_key is None:
        return ""
    try:
        record = dict(resolution.to_dict())
        record.pop(RECEIPT_FIELD, None)
        receipt = mint_resolution_receipt(
            record,
            session_id=session_id,
            paused_agent_id=paused_agent_id,
            reviewer_agent_id=reviewer_agent_id,
            signing_key=signing_key,
        )
    except Exception as exc:
        logger.warning(
            "dialectic receipt: minting failed for session %s (%s); "
            "resolution stored without receipt",
            session_id, exc,
        )
        return ""
    resolution.receipt = receipt
    return receipt


# ── verify ─────────────────────────────────────────────────────────────────


def _public_key_from_jwks(jwks: Mapping[str, Any], kid: Any) -> "Ed25519PublicKey":
    jwk = None
    for candidate in jwks.get("keys", []) or []:
        if isinstance(candidate, Mapping) and candidate.get("kid") == kid:
            jwk = candidate
            break
    if jwk is None:
        raise ReceiptError("unknown_kid", f"no key with kid {kid!r} in the supplied JWKS")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not jwk.get("x"):
        raise ReceiptError("unsupported_key", "JWK is not an Ed25519 OKP key")
    try:
        return Ed25519PublicKey.from_public_bytes(_b64u_decode(str(jwk["x"])))
    except Exception as exc:
        raise ReceiptError("unsupported_key", f"JWK public bytes are malformed: {exc}") from exc


def verify_resolution_receipt(
    receipt: str,
    record: Mapping[str, Any],
    *,
    jwks: Optional[Mapping[str, Any]] = None,
    public_key: Optional["Ed25519PublicKey"] = None,
    expected_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify ``receipt`` against ``record`` and return the receipt claims.

    Supply EITHER ``jwks`` (the deployment's published verification keys,
    selected by ``kid``) OR a single ``public_key``. Raises ``ReceiptError``
    with a stable ``code`` on any failure:

    ``malformed``          not a ``drr.v1`` envelope
    ``unsupported``        envelope parsed but wrong ``typ``/``v``
    ``no_verification_key`` neither ``jwks`` nor ``public_key`` given
    ``unknown_kid``        JWKS carries no key with the receipt's ``kid``
    ``unsupported_key``    the selected JWK is not usable Ed25519 material
    ``invalid_signature``  signature does not verify under that key
    ``record_mismatch``    signature verifies, but the record differs from
                           what was signed (tampered or wrong record)
    ``session_mismatch``   ``expected_session_id`` differs from the claim

    A return value means "this deployment issued exactly this record"; it is
    never an authorization and never evidence of a party's intent.
    """
    if not isinstance(receipt, str) or not receipt.startswith(RECEIPT_PREFIX):
        raise ReceiptError("malformed", "not a drr.v1 receipt")
    if not _CRYPTO_AVAILABLE:
        raise ReceiptError("crypto_unavailable", "Ed25519 receipts require the cryptography package")
    parts = receipt[len(RECEIPT_PREFIX):].split(".")
    if len(parts) != 2 or not all(parts):
        raise ReceiptError("malformed", "receipt must be drr.v1.<payload>.<signature>")
    payload_b64, signature_b64 = parts
    try:
        claims = json.loads(_b64u_decode(payload_b64))
        signature = _b64u_decode(signature_b64)
    except Exception as exc:
        raise ReceiptError("malformed", f"receipt is not decodable: {exc}") from exc
    if (
        not isinstance(claims, dict)
        or claims.get("typ") != RECEIPT_TYP
        or claims.get("v") != RECEIPT_VERSION
    ):
        raise ReceiptError("unsupported", "not a dialectic_resolution_receipt v1 payload")

    verify_key = public_key
    if verify_key is None:
        if not jwks:
            raise ReceiptError("no_verification_key", "supply jwks= or public_key=")
        verify_key = _public_key_from_jwks(jwks, claims.get("kid"))

    try:
        verify_key.verify(signature, (RECEIPT_PREFIX + payload_b64).encode("ascii"))
    except InvalidSignature as exc:
        raise ReceiptError("invalid_signature", "signature does not verify under this key") from exc
    except Exception as exc:
        raise ReceiptError("invalid_signature", f"signature check failed: {exc}") from exc

    if not isinstance(record, Mapping):
        raise ReceiptError("record_mismatch", "record must be a mapping")
    if record_sha256(record) != claims.get("record_sha256"):
        raise ReceiptError(
            "record_mismatch",
            "signature is authentic but the record is not the one that was signed",
        )
    if expected_session_id is not None and claims.get("session_id") != str(expected_session_id):
        raise ReceiptError(
            "session_mismatch",
            f"receipt is for session {claims.get('session_id')!r}, expected {expected_session_id!r}",
        )
    return claims


def is_authorization() -> bool:
    """A receipt never authorizes anything. Always False (greppable invariant)."""
    return False
