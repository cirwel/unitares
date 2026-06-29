"""Effect-binding grant primitive (governed-effect Phase 1 — content-binding).

Design: docs/proposals/governed-effect-effect-binding-v0.md (#1075).

This is the **Option B** primitive: a server-minted, short-TTL, single-use
grant that binds an authorization to an effect's *content*, so a captured
forwarded credential cannot be retargeted to a different effect (T1) or
replayed (T2, against a grant-only attacker). It does NOT close T3 (a
bearer+token holder still authors freely) — see the design's threat model.

What this module is / is NOT
----------------------------
- It is the **crypto primitive only**: mint + verify of the
  ``gnt.v1.<payload_b64>.<sig_b64>`` envelope. It is single-sourced and
  unit-tested here, and (this slice) wired into NOTHING — no endpoint mints
  it, no veto path verifies it. It is inert until a later wiring slice.
- ``verify_effect_grant`` does **not** consume the nonce. Single-use
  enforcement is the *store's* job at wire-time (an atomic
  ``INSERT ... ON CONFLICT DO NOTHING`` against ``consumed_nonces``); verify
  returns the nonce so the caller can consume it atomically in the same
  transaction as the veto. Putting consumption here would create a
  SELECT-then-INSERT TOCTOU.

Crypto stance (be honest — this is NOT the AIC)
-----------------------------------------------
The grant is a **symmetric HMAC** minted with the same fleet-wide secret as
the ``continuity_token`` (``_get_continuity_secret``). It is *issuer-
verifiable-only and copyable* — the exact property the AIC
(``agent_identity_credential.py``) was written to escape. It is justified not
by principled pedigree but by **thin authority**: a captured grant authorizes
*one* effect, *once*, for *seconds*, content-bound. The asymmetric upgrade
(Ed25519, per-agent enrolled key) is the design's Phase 2, not this.

Domain separation
------------------
The signature covers the domain-separated bytes ``gnt.v1.<payload_b64>``
(prefix included), mirroring the AIC's anti-confusion discipline. So even
though the grant shares the HMAC secret with the ``v1.`` continuity_token, a
grant and a token with identical payload bytes produce *different* signatures
and can never be cross-verified against each other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

# Single source for the HMAC secret — the design's "the secret it already
# holds". Domain separation (the gnt.v1. prefix in the signed bytes) keeps
# grants and continuity_tokens non-cross-verifiable despite the shared secret.
from src.mcp_handlers.identity.session import _get_continuity_secret

EFFECT_GRANT_VERSION = "gnt.v1"

# Phase 1 grants are effect-freshness, not identity-rebind: seconds, not the
# token's ~1h. A captured grant should be useless almost immediately.
_DEFAULT_GRANT_TTL_SECONDS = 30
_MIN_GRANT_TTL_SECONDS = 5


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _signing_input(payload_b64: str) -> bytes:
    """Domain-separated bytes the HMAC covers: 'gnt.v1.<payload_b64>'.

    Including the version prefix is what makes a grant non-cross-verifiable
    against a same-secret continuity_token (whose signing input is the bare
    payload_b64).
    """
    return f"{EFFECT_GRANT_VERSION}.{payload_b64}".encode()


@dataclass(frozen=True)
class EffectGrantVerification:
    """Result of verify_effect_grant. Fail-closed: ok is False on any doubt.

    ``nonce`` and ``exp`` are populated on a signature-valid grant (even one
    that then fails a field/freshness check) so a caller can log them; act on
    them only when ``ok`` is True. ``nonce`` is what the caller must atomically
    consume at the store to enforce single-use.
    """

    ok: bool
    reason: str
    nonce: Optional[str] = None
    exp: Optional[int] = None


def mint_effect_grant(
    *,
    aid: str,
    payload_sha256: str,
    surface: str,
    custody_mode: str,
    idempotency_key: str,
    ttl_seconds: int = _DEFAULT_GRANT_TTL_SECONDS,
    nonce: Optional[str] = None,
    _now: Optional[int] = None,
) -> Optional[str]:
    """Mint a single-use, content-bound effect grant.

    The bound tuple (design §5) is
    ``(aid, payload_sha256, surface, custody_mode, idempotency_key, nonce, exp)``
    — note ``effect_id`` is deliberately absent (server-assigned after propose;
    not the T1 anchor — content is). All fields are inside the signed payload,
    so tampering any one breaks the HMAC.

    Returns the ``gnt.v1.<payload_b64>.<sig_b64>`` string, or None if no secret
    is configured (caller must treat None as fail-closed, never as "skip").
    """
    secret = _get_continuity_secret()
    if not secret:
        return None
    if not (aid and payload_sha256 and surface and custody_mode and idempotency_key):
        return None

    now = int(time.time()) if _now is None else int(_now)
    ttl = max(_MIN_GRANT_TTL_SECONDS, int(ttl_seconds))
    grant_nonce = nonce or secrets.token_urlsafe(16)

    payload = {
        "v": 1,
        "aid": str(aid),
        "psha": str(payload_sha256),
        "surf": str(surface),
        "cust": str(custody_mode),
        "idem": str(idempotency_key),
        "nonce": grant_nonce,
        "iat": now,
        "exp": now + ttl,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = _b64url_encode(payload_json)
    sig = hmac.new(secret, _signing_input(payload_b64), hashlib.sha256).digest()
    return f"{EFFECT_GRANT_VERSION}.{payload_b64}.{_b64url_encode(sig)}"


def verify_effect_grant(
    grant: Optional[str],
    *,
    aid: str,
    payload_sha256: str,
    surface: str,
    custody_mode: str,
    idempotency_key: str,
    _now: Optional[int] = None,
) -> EffectGrantVerification:
    """Verify a grant covers *exactly* the effect being cleared. Fail-closed.

    Checks, in order, all required to pass:
      1. parses as ``gnt.v1.<payload_b64>.<sig_b64>``;
      2. HMAC over the domain-separated signing input matches (constant-time);
      3. not expired (``exp`` vs now) — effect-freshness;
      4. every bound field equals the caller's *expected* value taken from the
         live envelope: ``aid`` (identity), ``psha`` (T1 content anchor),
         ``surf``/``cust`` (content identity), ``idem`` (audit-trail binding).

    Does NOT consume the nonce — returns it for the caller to consume atomically
    at the store. A True ``ok`` means "this grant authorizes this exact effect";
    single-use is still owed by the caller.
    """
    if not grant or not isinstance(grant, str):
        return EffectGrantVerification(False, "grant_absent")
    secret = _get_continuity_secret()
    if not secret:
        return EffectGrantVerification(False, "no_secret")

    # The version token (gnt.v1) itself contains a dot, so parse the prefix
    # literally rather than splitting on "." (payload_b64/sig_b64 are
    # base64url and carry no dots). Mirrors the aic.v2. envelope discipline.
    prefix = EFFECT_GRANT_VERSION + "."
    if not grant.startswith(prefix):
        return EffectGrantVerification(False, "wrong_version")
    try:
        payload_b64, sig_b64 = grant[len(prefix):].split(".", 1)
    except ValueError:
        return EffectGrantVerification(False, "malformed")

    expected_sig = _b64url_encode(
        hmac.new(secret, _signing_input(payload_b64), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_sig, sig_b64):
        return EffectGrantVerification(False, "bad_signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode())
        if not isinstance(payload, dict):
            return EffectGrantVerification(False, "bad_payload")
    except Exception:
        return EffectGrantVerification(False, "bad_payload")

    nonce = payload.get("nonce")
    exp = payload.get("exp")
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return EffectGrantVerification(False, "bad_exp", nonce=nonce)

    now = int(time.time()) if _now is None else int(_now)
    if now >= exp_int:
        return EffectGrantVerification(False, "expired", nonce=nonce, exp=exp_int)

    # Field binding — each mismatch means the grant authorizes a *different*
    # effect (or proposer, or key) than the one being cleared. T1 lives here.
    expected = {
        "aid": str(aid),
        "psha": str(payload_sha256),
        "surf": str(surface),
        "cust": str(custody_mode),
        "idem": str(idempotency_key),
    }
    for field, want in expected.items():
        if str(payload.get(field)) != want:
            return EffectGrantVerification(False, f"mismatch_{field}", nonce=nonce, exp=exp_int)

    return EffectGrantVerification(True, "ok", nonce=nonce, exp=exp_int)
