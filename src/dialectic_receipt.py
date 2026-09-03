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
the resolution dict as persisted at the terminal ``resolved`` write. It is
signed with the deployment's server-to-world attestation key, the same seed
and JWKS shape as the Agent Identity Credential
(``src/identity/agent_identity_credential.py``), so a peer who has pinned this
deployment's public key verifies it offline with no ``api_key``, no database,
and no server round-trip.

What a valid receipt proves, and what it does not
-------------------------------------------------
Proves: the holder of the private half of ``kid`` persisted, as ``resolved``,
a record whose listed fields (``record_fields``) had exactly the values the
verifier was handed, for this session and these party identifiers, and it
attached the receipt at ``iat``.

Does not prove: that either party *intended* the resolution, or that the
parties' symmetric signatures are valid — a peer cannot check those and the
receipt does not claim to. ``both_signatures_present`` reports only that two
non-empty signature strings were stored; note that an LLM-assisted session
signs ``signature_a`` with a fallback key derived from the agent uuid when no
api_key is on file, and leaves ``signature_b`` empty. Party-level
non-repudiation would need party-held asymmetric keys, a separate decision
(shelved 2026-04-19).

There is no expiry and no revocation. ``iat`` is a claim by the signer, not a
checked bound: a leaked seed lets its holder mint back-dated receipts until
the key is rotated out of every peer's pinned set. The transparency-log
construction the threat model names is what would bound that; it is not built
here, and this module does not pretend otherwise.

Posture
-------
Off by default, behind two settings: ``UNITARES_DIALECTIC_RESOLUTION_RECEIPTS``
(a dedicated issuance gate, so that configuring the identity-attestation key
for another purpose cannot switch receipts on) and ``UNITARES_AIC_SIGNING_KEY``
(the key). A receipt is minted only at the terminal ``resolved`` write (never
at finalization, which precedes the hard-limit and self-review gates), never
for a ``failed`` write, and it is cleared again unless that write is confirmed.
The receipt never modifies the record it signs; with issuance off nothing
changes at all: no field is added to the stored record and
``Resolution.hash()`` is unchanged. A configured-but-unusable key logs a
warning and stores the resolution without a receipt: persisting a resolution
is a liveness path for a paused agent and must not fail on attestation
plumbing. An absent receipt therefore means "issuance off, or no usable key,
or the write was not confirmed"; it says nothing about the record.

Canonical form (for a verifier in any language)
-----------------------------------------------
The receipt names the fields it covers (``record_fields``), which must include
the eight drr.v1 resolution fields. ``record_sha256`` is SHA-256 over the UTF-8
JSON encoding of ``{field: record[field]}`` for exactly those fields, with keys
sorted by code point, separators ``,`` and ``:`` with no whitespace, and
non-ASCII characters emitted raw (not escaped). For this record shape — fixed
ASCII keys; string, integer and list-of-string values; no floats — that is
byte-identical to RFC 8785 (JCS). One equivalence is admitted before encoding:
string entries of ``conditions`` are stripped of surrounding whitespace and
empty strings are dropped, which is exactly what the server's read path does
when it serves a record, so the stored row and the served copy hash
identically without the receipt ever touching the stored record; no other
value is altered and types are never coerced. Fields present in the record but
not listed are ignored, so later schema additions do not invalidate earlier
receipts; a listed field missing from the presented record is a mismatch, and
the receipt's own ``signature_version`` and ``both_signatures_present`` claims
must agree with the record. The signed message is the ASCII bytes of
``"drr.v1." + payload_b64url``; the receipt is
``"drr.v1." + payload_b64url + "." + signature_b64url`` with unpadded
base64url throughout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
RECEIPT_STATUS = "resolved"
RECEIPTS_ENV = "UNITARES_DIALECTIC_RESOLUTION_RECEIPTS"
REQUIRED_RECORD_FIELDS = (
    "action", "conditions", "reasoning", "root_cause",
    "signature_a", "signature_b", "signature_version", "timestamp",
)
_PROFILE = {"alg": "EdDSA", "stance": "descriptive", "authorizes": [], "status": RECEIPT_STATUS}
_TRUTHY = {"1", "true", "yes", "on"}


def receipts_enabled() -> bool:
    """Mint dialectic resolution receipts (drr.v1) at the terminal resolved write; default off, and the AIC attestation key must be configured as well.

    A dedicated gate, so that configuring the identity-attestation key for some
    other purpose can never switch receipt issuance on as a side effect.
    """
    return (os.getenv(RECEIPTS_ENV) or "").strip().lower() in _TRUTHY


class ReceiptError(ValueError):
    """A receipt did not verify. ``code`` is a stable, greppable reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ── canonical form ─────────────────────────────────────────────────────────


def covered_fields(record: Mapping[str, Any]) -> List[str]:
    """Every field of the record except the receipt itself, sorted."""
    return sorted(k for k in record.keys() if k != RECEIPT_FIELD)


def _canonical_conditions(value: Any) -> Any:
    """String conditions stripped, empty strings dropped; every other item untouched.

    This is the one equivalence the digest admits, and it exists so the row as
    stored and the copy the server serves (whose read path applies exactly this
    stripping) hash identically without the receipt ever altering the stored
    record. Types are never coerced: ``[1]`` and ``["1"]`` remain different.
    """
    if not isinstance(value, (list, tuple)):
        return value
    out = []
    for item in value:
        if isinstance(item, str):
            item = item.strip()
            if not item:
                continue
        out.append(item)
    return out


def canonical_record_bytes(record: Mapping[str, Any], fields: Sequence[str]) -> bytes:
    """The exact bytes a receipt commits to. See the module docstring."""
    missing = [f for f in fields if f not in record]
    if missing:
        raise ReceiptError("record_mismatch", f"record lacks covered field(s): {', '.join(missing)}")
    reduced = {f: record[f] for f in fields}
    if "conditions" in reduced:
        reduced["conditions"] = _canonical_conditions(reduced["conditions"])
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_sha256(record: Mapping[str, Any], fields: Sequence[str]) -> str:
    return hashlib.sha256(canonical_record_bytes(record, fields)).hexdigest()


def _configured_issuer() -> Optional[str]:
    """The deployment's declared issuer name, if it has one.

    Reuses the lease plane's issuer identity so a receipt names the same
    deployment its lease attestations do; no new configuration surface.
    """
    try:
        from src.lease_attestation import LeaseAttestationError, configured_issuer
    except Exception:  # pragma: no cover
        return None
    try:
        return configured_issuer()
    except LeaseAttestationError:
        return None
    except Exception:  # pragma: no cover - never let issuer lookup block a mint
        return None


# ── mint ───────────────────────────────────────────────────────────────────


def mint_resolution_receipt(
    record: Mapping[str, Any],
    *,
    session_id: str,
    paused_agent_id: str,
    reviewer_agent_id: Optional[str],
    signing_key: "Ed25519PrivateKey",
    issuer: Optional[str] = None,
    now: Optional[int] = None,
) -> str:
    """Sign ``record`` with the deployment key and return the ``drr.v1`` receipt.

    The claim set is small and self-describing: ``authorizes`` is empty,
    ``stance`` is ``descriptive`` and ``status`` is ``resolved``; the verifier
    enforces all three, so a token that says anything else is not a receipt.
    ``both_signatures_present`` is what the deployment observed in the stored
    record (two non-empty signature strings), not a validity claim.
    """
    if not _CRYPTO_AVAILABLE:
        raise ReceiptError("crypto_unavailable", "Ed25519 receipts require the cryptography package")
    if not session_id:
        raise ReceiptError("missing_session_id", "a receipt must be bound to a session id")
    fields = covered_fields(record)
    missing = [f for f in REQUIRED_RECORD_FIELDS if f not in fields]
    if missing:
        raise ReceiptError("record_mismatch", f"record lacks required field(s): {', '.join(missing)}")
    payload: Dict[str, Any] = {
        "v": RECEIPT_VERSION,
        "typ": RECEIPT_TYP,
        **_PROFILE,
        "kid": key_id(signing_key.public_key()),
        "iss": issuer,
        "iat": int(now if now is not None else time.time()),
        "session_id": str(session_id),
        "paused_agent_id": str(paused_agent_id or ""),
        "reviewer_agent_id": str(reviewer_agent_id) if reviewer_agent_id else None,
        "record_fields": fields,
        "record_sha256": record_sha256(record, fields),
        "signature_version": record.get("signature_version"),
        "both_signatures_present": bool(record.get("signature_a")) and bool(record.get("signature_b")),
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
    """Mint and attach a receipt when issuance is on and the key is configured.

    Both ``UNITARES_DIALECTIC_RESOLUTION_RECEIPTS`` and the attestation key are
    required; either absent means ``""``. The record is never modified.

    Returns the receipt, or ``""`` when issuance is off, no key is configured,
    or minting could not proceed. Never raises: a resolution must persist even
    if attestation plumbing is broken, and the failure is logged at WARNING so
    it is not silent.
    """
    if not receipts_enabled():
        return ""
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
            issuer=_configured_issuer(),
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


def merge_jwks(documents: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Combine several JWKS documents (for example a current and a retired
    key) into one verification set. Duplicate ``kid`` entries keep the first."""
    seen: Dict[str, Mapping[str, Any]] = {}
    for doc in documents:
        if not isinstance(doc, Mapping) or not isinstance(doc.get("keys"), list):
            raise ReceiptError("malformed_jwks", "a JWKS must be an object with a 'keys' list")
        for jwk in doc["keys"]:
            if not isinstance(jwk, Mapping):
                raise ReceiptError("malformed_jwks", "every JWKS entry must be an object")
            kid = jwk.get("kid")
            if isinstance(kid, str) and kid not in seen:
                seen[kid] = jwk
    return {"keys": list(seen.values())}


def _public_key_from_jwks(jwks: Any, kid: Any) -> "Ed25519PublicKey":
    if not isinstance(jwks, Mapping) or not isinstance(jwks.get("keys"), list):
        raise ReceiptError("malformed_jwks", "a JWKS must be an object with a 'keys' list")
    jwk = None
    for candidate in jwks["keys"]:
        if isinstance(candidate, Mapping) and candidate.get("kid") == kid:
            jwk = candidate
            break
    if jwk is None:
        raise ReceiptError("unknown_kid", f"no key with kid {kid!r} in the supplied JWKS")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
        raise ReceiptError("unsupported_key", "JWK is not an Ed25519 OKP key")
    try:
        return Ed25519PublicKey.from_public_bytes(_b64u_decode(jwk["x"]))
    except Exception as exc:
        raise ReceiptError("unsupported_key", f"JWK public bytes are malformed: {exc}") from exc


def _check_profile(claims: Mapping[str, Any]) -> None:
    for key, expected in _PROFILE.items():
        if claims.get(key) != expected:
            raise ReceiptError(
                "profile_mismatch",
                f"claim {key!r} is {claims.get(key)!r}, a receipt requires {expected!r}",
            )
    fields = claims.get("record_fields")
    if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
        raise ReceiptError("profile_mismatch", "record_fields must be a non-empty list of field names")
    if not set(REQUIRED_RECORD_FIELDS) <= set(fields):
        raise ReceiptError("profile_mismatch", "record_fields must cover the drr.v1 resolution fields")
    if not isinstance(claims.get("record_sha256"), str) or not isinstance(claims.get("kid"), str):
        raise ReceiptError("profile_mismatch", "record_sha256 and kid must be strings")
    if not isinstance(claims.get("session_id"), str) or not claims.get("session_id"):
        raise ReceiptError("profile_mismatch", "session_id must be a non-empty string")


def verify_resolution_receipt(
    receipt: str,
    record: Mapping[str, Any],
    *,
    jwks: Optional[Mapping[str, Any]] = None,
    public_key: Optional["Ed25519PublicKey"] = None,
    expected_session_id: Optional[str] = None,
    expected_issuer: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify ``receipt`` against ``record`` and return the receipt claims.

    Supply EITHER ``jwks`` (the deployment's published verification keys,
    selected by ``kid``) OR a single ``public_key`` (whose ``kid`` must match
    the claim). Raises ``ReceiptError`` with a stable ``code``:

    ``crypto_unavailable``  the cryptography package is not installed
    ``malformed``           not a ``drr.v1`` envelope
    ``unsupported``         envelope parsed but wrong ``typ``/``v``
    ``profile_mismatch``    a claim contradicts the receipt profile (``alg``,
                            ``stance``, ``authorizes``, ``status``, field list)
    ``no_verification_key`` neither ``jwks`` nor ``public_key`` given
    ``malformed_jwks``      the JWKS is not an object with a ``keys`` list
    ``unknown_kid``         JWKS carries no key with the receipt's ``kid``
    ``unsupported_key``     the selected JWK is not usable Ed25519 material
    ``kid_mismatch``        ``public_key`` is not the key named by ``kid``
    ``invalid_signature``   signature does not verify under that key
    ``record_mismatch``     signature verifies, but the covered fields differ
                            from what was signed (tampered or wrong record)
    ``claim_mismatch``      a redundant claim (``signature_version``,
                            ``both_signatures_present``) contradicts the record
    ``session_mismatch``    ``expected_session_id`` differs from the claim
    ``issuer_mismatch``     ``expected_issuer`` differs from the claim

    A return value means "this key's holder persisted exactly these field
    values as a resolved record"; it is never an authorization and never
    evidence of a party's intent.
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
    _check_profile(claims)

    verify_key = public_key
    if verify_key is None:
        if jwks is None:
            raise ReceiptError("no_verification_key", "supply jwks= or public_key=")
        verify_key = _public_key_from_jwks(jwks, claims["kid"])
    elif key_id(verify_key) != claims["kid"]:
        raise ReceiptError("kid_mismatch", "the supplied public key is not the key named by kid")

    try:
        verify_key.verify(signature, (RECEIPT_PREFIX + payload_b64).encode("ascii"))
    except InvalidSignature as exc:
        raise ReceiptError("invalid_signature", "signature does not verify under this key") from exc
    except Exception as exc:
        raise ReceiptError("invalid_signature", f"signature check failed: {exc}") from exc

    if not isinstance(record, Mapping):
        raise ReceiptError("record_mismatch", "record must be a mapping")
    if record_sha256(record, claims["record_fields"]) != claims["record_sha256"]:
        raise ReceiptError(
            "record_mismatch",
            "signature is authentic but the covered fields are not the ones that were signed",
        )
    if claims.get("signature_version") != record.get("signature_version"):
        raise ReceiptError("claim_mismatch", "signature_version claim disagrees with the record")
    if claims.get("both_signatures_present") != (bool(record.get("signature_a")) and bool(record.get("signature_b"))):
        raise ReceiptError("claim_mismatch", "both_signatures_present claim disagrees with the record")
    if expected_session_id is not None and claims["session_id"] != str(expected_session_id):
        raise ReceiptError(
            "session_mismatch",
            f"receipt is for session {claims['session_id']!r}, expected {expected_session_id!r}",
        )
    if expected_issuer is not None and claims.get("iss") != expected_issuer:
        raise ReceiptError(
            "issuer_mismatch",
            f"receipt names issuer {claims.get('iss')!r}, expected {expected_issuer!r}",
        )
    return claims
