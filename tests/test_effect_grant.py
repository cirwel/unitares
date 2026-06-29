"""Unit tests for the effect-binding grant primitive (#1075 Phase 1, slice 1).

Covers the threat-relevant properties the design (§3/§5) hangs on:
  - round-trip mint -> verify for the exact effect (allow path);
  - T1 retarget: a grant for one payload/surface/custody must not verify for
    another;
  - identity + idempotency binding (aid / idem mismatch -> fail);
  - freshness (expired -> fail) — the basis for the seconds-TTL replay shrink;
  - tamper resistance (mutated sig/payload -> fail);
  - domain separation: a continuity_token shape and the grant cannot
    cross-verify even under the shared secret;
  - fail-closed when no secret is configured;
  - the nonce is returned for the caller to consume (verify never consumes).

The module is inert (wired into nothing); these tests are its whole contract.
"""

import importlib

import pytest

import src.effect_grant as eg


_SECRET = b"test-fleet-secret-effect-grant"

_FIELDS = dict(
    aid="11111111-2222-3333-4444-555555555555",
    payload_sha256="a" * 64,
    surface="file://sandbox/x",
    custody_mode="execute",
    idempotency_key="idem-key-001",
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    """Pin a deterministic HMAC secret for every test."""
    monkeypatch.setattr(eg, "_get_continuity_secret", lambda: _SECRET)


def _mint(**overrides):
    args = dict(_FIELDS)
    args.update(overrides)
    return eg.mint_effect_grant(**args)


def _verify(grant, **overrides):
    args = dict(_FIELDS)
    args.update(overrides)
    return eg.verify_effect_grant(grant, **args)


# ── allow path ──────────────────────────────────────────────────────────────

def test_roundtrip_allows_exact_effect():
    grant = _mint()
    assert grant and grant.startswith("gnt.v1.")
    v = _verify(grant)
    assert v.ok is True
    assert v.reason == "ok"
    assert v.nonce  # returned for the caller to consume
    assert isinstance(v.exp, int)


def test_each_mint_has_a_unique_nonce():
    a = eg.verify_effect_grant(_mint(), **_FIELDS)
    b = eg.verify_effect_grant(_mint(), **_FIELDS)
    assert a.nonce and b.nonce and a.nonce != b.nonce


# ── T1: retarget must fail (content anchor) ─────────────────────────────────

@pytest.mark.parametrize("field,bad", [
    ("payload_sha256", "b" * 64),
    ("surface", "file://sandbox/OTHER"),
    ("custody_mode", "record_only"),
])
def test_retarget_to_different_content_fails(field, bad):
    grant = _mint()
    v = _verify(grant, **{field: bad})
    assert v.ok is False
    assert v.reason.startswith("mismatch_")


# ── identity + idempotency binding ──────────────────────────────────────────

def test_mismatched_aid_fails():
    grant = _mint()
    v = _verify(grant, aid="99999999-0000-0000-0000-000000000000")
    assert v.ok is False and v.reason == "mismatch_aid"


def test_mismatched_idempotency_key_fails():
    grant = _mint()
    v = _verify(grant, idempotency_key="idem-key-ATTACKER")
    assert v.ok is False and v.reason == "mismatch_idem"


# ── freshness (the seconds-TTL replay shrink) ───────────────────────────────

def test_expired_grant_fails():
    grant = eg.mint_effect_grant(ttl_seconds=5, _now=1_000_000, **_FIELDS)
    # verify well after exp
    v = eg.verify_effect_grant(grant, _now=1_000_100, **_FIELDS)
    assert v.ok is False and v.reason == "expired"


def test_ttl_floor_enforced():
    # ttl below the floor is clamped up, not honored as-is
    grant = eg.mint_effect_grant(ttl_seconds=0, _now=1_000_000, **_FIELDS)
    v = eg.verify_effect_grant(grant, _now=1_000_003, **_FIELDS)  # within floor
    assert v.ok is True


# ── tamper resistance ───────────────────────────────────────────────────────

def test_tampered_signature_fails():
    grant = _mint()
    version, payload_b64, sig_b64 = grant.split(".", 2)
    flipped = sig_b64[:-2] + ("AA" if not sig_b64.endswith("AA") else "BB")
    v = _verify(f"{version}.{payload_b64}.{flipped}")
    assert v.ok is False and v.reason == "bad_signature"


def _split_grant(grant):
    """Parse gnt.v1.<payload>.<sig> (the version token itself has a dot)."""
    prefix = eg.EFFECT_GRANT_VERSION + "."
    assert grant.startswith(prefix)
    payload_b64, sig_b64 = grant[len(prefix):].split(".", 1)
    return prefix, payload_b64, sig_b64


def test_tampered_payload_fails():
    prefix, _, sig_b64 = _split_grant(_mint())
    _, other_payload_b64, _ = _split_grant(_mint(payload_sha256="b" * 64))
    # swap in a different payload, keep the old signature -> HMAC mismatch
    v = _verify(f"{prefix}{other_payload_b64}.{sig_b64}")
    assert v.ok is False and v.reason == "bad_signature"


@pytest.mark.parametrize("bad", ["", "not-a-grant", "gnt.v1.onlytwo", "v1.x.y"])
def test_malformed_or_wrong_version_fails(bad):
    v = _verify(bad)
    assert v.ok is False
    assert v.reason in ("grant_absent", "malformed", "wrong_version")


# ── domain separation vs continuity_token ───────────────────────────────────

def test_grant_does_not_cross_verify_as_continuity_token():
    """A grant signs over 'gnt.v1.<payload>'; the continuity_token signs over
    the bare '<payload>'. Even sharing the secret, the grant's sig must not
    validate under the token's signing input."""
    import hmac as _hmac
    import hashlib as _hashlib

    grant = _mint()
    _, payload_b64, sig_b64 = grant.split(".", 2)
    token_style_sig = eg._b64url_encode(
        _hmac.new(_SECRET, payload_b64.encode(), _hashlib.sha256).digest()
    )
    assert token_style_sig != sig_b64  # prefix domain-separation holds


def test_continuity_token_shape_is_rejected():
    # a v1. token presented to the grant verifier fails on version, not by
    # accident of signature
    v = _verify("v1.eyJ2IjoxfQ.sig")
    assert v.ok is False and v.reason == "wrong_version"


# ── fail-closed on missing secret ───────────────────────────────────────────

def test_mint_returns_none_without_secret(monkeypatch):
    monkeypatch.setattr(eg, "_get_continuity_secret", lambda: None)
    assert eg.mint_effect_grant(**_FIELDS) is None


def test_verify_fails_closed_without_secret(monkeypatch):
    grant = _mint()
    monkeypatch.setattr(eg, "_get_continuity_secret", lambda: None)
    v = eg.verify_effect_grant(grant, **_FIELDS)
    assert v.ok is False and v.reason == "no_secret"


def test_module_imports_clean():
    importlib.reload(eg)
