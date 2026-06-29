"""Proof-tests for POST /v1/effect-grant (effect-binding Phase 1 mint endpoint).

The endpoint mints a single-use, content-bound grant ONLY for a proposer that
re-certifies as a `strong` identity (the §7 gate), and ONLY when the binding
feature flag is on (inert/501 otherwise). Proven here so neither can be a silent
no-op:

  - flag OFF  → 501 (inert by default; a premature caller is refused).
  - no/aid-mismatch token → 403 (no grant for a non-strong proposer).
  - missing content field → 422.
  - valid strong token + all fields → 200 with a grant that VERIFIES for that
    exact effect (round-trip through the real primitive) and FAILS for a
    different payload (T1 holds at the endpoint boundary).

Tokens are minted with the real `create_continuity_token` under a test HMAC
secret, so the §7 path is exercised end-to-end (real HMAC + exp).
"""

import os
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_effect_grant
from src.effect_grant import verify_effect_grant
from src.mcp_handlers.identity import session as session_mod

PROPOSER = "00000000-0000-0000-0000-0000000000aa"
OTHER_UUID = "00000000-0000-0000-0000-0000000000bb"
TEST_SECRET = "test-continuity-secret-0123456789abcdef"

_FIELDS = {
    "payload_sha256": "a" * 64,
    "surface": "file://sandbox/x",
    "custody_mode": "execute",
    "idempotency_key": "idem-key-001",
}


@pytest.fixture(autouse=True)
def _env():
    """Shared secret for mint+verify, binding flag ON, ambient bearers cleared."""
    with patch.dict(
        os.environ,
        {
            "UNITARES_CONTINUITY_TOKEN_SECRET": TEST_SECRET,
            "UNITARES_GOVERNED_EFFECT_BINDING": "1",
            "UNITARES_MCP_BEARER_TOKENS": "",
            "UNITARES_HTTP_API_TOKEN": "",
        },
    ):
        yield


def _token(aid=PROPOSER):
    return session_mod.create_continuity_token(aid, f"sid-{aid[:8]}")


def _client():
    app = Starlette(routes=[Route("/v1/effect-grant", http_effect_grant, methods=["POST"])])
    return TestClient(app)


def _post(body):
    return _client().post("/v1/effect-grant", json=body)


def _full_body(token="__valid__", **overrides):
    body = {"proposer_agent_uuid": PROPOSER, **_FIELDS}
    if token == "__valid__":
        token = _token()
    if token is not None:
        body["proposer_continuity_token"] = token
    body.update(overrides)
    return body


# ── inert by default ─────────────────────────────────────────────────────────

def test_disabled_flag_returns_501():
    with patch.dict(os.environ, {"UNITARES_GOVERNED_EFFECT_BINDING": "0"}):
        r = _post(_full_body())
    assert r.status_code == 501
    assert r.json()["error"] == "binding_not_enabled"


# ── §7 gate: a grant is only minted for a strong-recertified proposer ─────────

def test_no_token_is_refused():
    r = _post(_full_body(token=None))
    assert r.status_code == 403
    assert r.json()["error"] == "tier_recert_failed"


def test_aid_mismatch_token_is_refused():
    # a valid token whose aid != the claimed proposer must not yield a grant
    r = _post(_full_body(token=_token(aid=OTHER_UUID)))
    assert r.status_code == 403
    assert r.json()["error"] == "tier_recert_failed"


# ── schema ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("drop", ["payload_sha256", "surface", "custody_mode", "idempotency_key"])
def test_missing_content_field_is_422(drop):
    body = _full_body()
    body.pop(drop)
    r = _post(body)
    assert r.status_code == 422
    assert r.json()["error"] == "schema_invalid"
    assert drop in r.json()["detail"]


# ── allow path: minted grant verifies for THIS effect, not another (T1) ──────

def test_strong_proposer_gets_a_grant_that_verifies():
    r = _post(_full_body())
    assert r.status_code == 200
    grant = r.json()["grant"]
    assert grant.startswith("gnt.v1.")

    # round-trip through the real primitive: the grant authorizes exactly the
    # effect it was minted for, bound to the proposer.
    v = verify_effect_grant(grant, aid=PROPOSER, **_FIELDS)
    assert v.ok is True
    assert v.nonce

    # T1 at the boundary: the same grant must NOT verify for a different payload
    bad = verify_effect_grant(grant, aid=PROPOSER, **{**_FIELDS, "payload_sha256": "b" * 64})
    assert bad.ok is False


def test_custom_ttl_is_honored_within_floor():
    r = _post(_full_body(ttl_seconds=120))
    assert r.status_code == 200
    grant = r.json()["grant"]
    v = verify_effect_grant(grant, aid=PROPOSER, **_FIELDS)
    assert v.ok is True


# ── fail-closed when minting is impossible ───────────────────────────────────

def test_mint_returning_none_is_503():
    # valid token (so §7 passes) but the primitive can't mint → fail closed
    with patch("src.effect_grant.mint_effect_grant", return_value=None):
        r = _post(_full_body())
    assert r.status_code == 503
    assert r.json()["error"] == "grant_mint_unavailable"
