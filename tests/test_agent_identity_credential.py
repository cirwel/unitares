"""Tests for the Agent Identity Credential (AIC) attestation prototype.

Grounded in docs/ontology/identity.md (three stances, five-layer taxonomy) and
plan.md S19 (B-strict server-verifiable attestation). The AIC is a DESCRIPTIVE
attestation, third-party-verifiable via published public key, and must never
behave like the Performative `continuity_token` (no session proof, no resume
authority, non-interchangeable envelope).

Timestamps are passed explicitly (no wall-clock dependence) so the validity-
window assertions are deterministic.
"""

import pytest

cryptography = pytest.importorskip("cryptography")  # skip on minimal installs

from src.identity.agent_identity_credential import (  # noqa: E402
    AIC_PREFIX,
    AICError,
    export_public_jwks,
    generate_signing_key_seed,
    is_resume_credential,
    key_id,
    load_signing_key,
    mint_identity_attestation,
    verify_identity_attestation,
)

T0 = 1_782_000_000  # fixed "now" for deterministic validity-window tests


@pytest.fixture
def signing_key():
    return load_signing_key(generate_signing_key_seed())


def _mint(signing_key, **overrides):
    base = dict(
        uuid="dadfe388-2655-4682-b99d-9421adefa24a",
        structured_agent_id="Claude_20260623",
        role_family="ephemeral",
        substrate_class=None,
        trust_tier=1,
        observation_count=5,
        lineage_state="no_lineage_declared",
        signing_key=signing_key,
        now=T0,
        ttl_seconds=3600,
    )
    base.update(overrides)
    return mint_identity_attestation(**base)


# ── shape + round-trip ─────────────────────────────────────────────────────

def test_token_uses_disjoint_aic_v2_envelope(signing_key):
    token = _mint(signing_key)
    assert token.startswith("aic.v2.")
    # Three dot-segments after the version: aic . v2 . payload . sig
    assert token.count(".") == 3


def test_round_trip_with_signing_key_public_half(signing_key):
    token = _mint(signing_key)
    claims = verify_identity_attestation(
        token, public_key=signing_key.public_key(), now=T0 + 10
    )
    assert claims is not None
    assert claims["uuid"] == "dadfe388-2655-4682-b99d-9421adefa24a"
    assert claims["structured_agent_id"] == "Claude_20260623"
    assert claims["trust_tier"] == 1
    assert claims["observation_count"] == 5


def test_third_party_verifies_offline_with_published_jwks(signing_key):
    """The core win: a party holding ONLY the public JWKS verifies the issuer's
    attestation without contacting the issuer."""
    token = _mint(signing_key)
    jwks = export_public_jwks(signing_key)  # what /.well-known would serve
    claims = verify_identity_attestation(token, jwks=jwks, now=T0 + 10)
    assert claims is not None
    assert claims["kid"] == jwks["keys"][0]["kid"]
    assert jwks["keys"][0]["crv"] == "Ed25519"
    assert jwks["keys"][0]["alg"] == "EdDSA"


# ── self-describing authority boundary (ontology: descriptive, not bearer) ──

def test_attestation_carries_no_session_proof_and_no_authority(signing_key):
    claims = verify_identity_attestation(
        _mint(signing_key), public_key=signing_key.public_key(), now=T0
    )
    assert claims["stance"] == "descriptive"
    assert claims["resume_capable"] is False
    assert claims["authorizes"] == []
    # No session/continuity field may ride along — that is what makes copying
    # an AIC harmless where copying a continuity_token is not.
    assert "sid" not in claims
    assert "client_session_id" not in claims


def test_is_resume_credential_is_always_false():
    assert is_resume_credential() is False


# ── non-interchangeable with continuity_token ──────────────────────────────

def test_verify_rejects_a_continuity_token_shaped_string(signing_key):
    # continuity_token shape is v1.<payload>.<sig> — must not cross-verify.
    fake_continuity = "v1.eyJhaWQiOiJ4In0.c2ln"
    assert verify_identity_attestation(
        fake_continuity, public_key=signing_key.public_key(), now=T0
    ) is None


def test_verify_rejects_garbage_and_non_strings(signing_key):
    pk = signing_key.public_key()
    for bad in ["", "aic.v2.", "aic.v2.onlyonepart", None, 12345, "aic.v3.x.y"]:
        assert verify_identity_attestation(bad, public_key=pk, now=T0) is None


# ── tamper / wrong-key detection ───────────────────────────────────────────

def test_tampered_payload_fails_verification(signing_key):
    token = _mint(signing_key)
    prefix, payload_b64, sig_b64 = token[: len(AIC_PREFIX)], *token[len(AIC_PREFIX):].split(".")
    # Flip a character in the payload segment.
    flipped = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
    tampered = f"{prefix}{flipped}.{sig_b64}"
    assert verify_identity_attestation(
        tampered, public_key=signing_key.public_key(), now=T0
    ) is None


def test_wrong_key_rejects(signing_key):
    other = load_signing_key(generate_signing_key_seed())
    token = _mint(signing_key)
    assert verify_identity_attestation(
        token, public_key=other.public_key(), now=T0
    ) is None
    # And a JWKS that lacks the signing kid cannot resolve a verification key.
    assert verify_identity_attestation(
        token, jwks=export_public_jwks(other), now=T0
    ) is None


# ── validity window + revocation ───────────────────────────────────────────

def test_expired_attestation_rejected(signing_key):
    token = _mint(signing_key, ttl_seconds=60)
    pk = signing_key.public_key()
    assert verify_identity_attestation(token, public_key=pk, now=T0 + 30) is not None
    # Past exp + skew.
    assert verify_identity_attestation(token, public_key=pk, now=T0 + 60 + 31) is None


def test_not_yet_valid_rejected(signing_key):
    token = _mint(signing_key, now=T0)
    # Well before nbf (minus skew).
    assert verify_identity_attestation(
        token, public_key=signing_key.public_key(), now=T0 - 100
    ) is None


def test_revoked_jti_rejected(signing_key):
    token = _mint(signing_key, jti="cred-123")
    pk = signing_key.public_key()
    assert verify_identity_attestation(token, public_key=pk, now=T0) is not None
    assert verify_identity_attestation(
        token, public_key=pk, now=T0, revoked_jti={"cred-123"}
    ) is None


# ── key id stability + misconfig ───────────────────────────────────────────

def test_kid_is_stable_for_a_key(signing_key):
    assert key_id(signing_key.public_key()) == key_id(signing_key.public_key())
    assert export_public_jwks(signing_key)["keys"][0]["kid"] == key_id(
        signing_key.public_key()
    )


def test_missing_signing_key_raises(monkeypatch):
    monkeypatch.delenv("UNITARES_AIC_SIGNING_KEY", raising=False)
    with pytest.raises(AICError):
        load_signing_key()


def test_mint_requires_uuid(signing_key):
    with pytest.raises(AICError):
        mint_identity_attestation(uuid="", signing_key=signing_key, now=T0)
