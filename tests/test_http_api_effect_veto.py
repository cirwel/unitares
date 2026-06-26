"""Proof-tests for POST /v1/effect-veto (governed-effect §6 verdict veto + §7
strong-tier re-certification).

Two gates compose; either trips the veto. The gates that matter, both proven
here so neither can be a silent no-op:

  §6 — a FLAGGED proposer (high-risk/paused) is vetoed.
  §7 — a proposer that does NOT re-certify as a `strong` identity (no token,
       bad/expired token, or a token whose aid ≠ the claimed proposer) is
       vetoed, EVEN with a clean §6 verdict. A valid strong token flips it to
       allowed — so §7 provably blocks AND provably allows.

Governance state is mocked (deterministic, no DB pollution). Continuity tokens
are minted with the real `create_continuity_token` under a test HMAC secret set
for the whole module, so the §7 path is exercised end-to-end (real HMAC + exp),
not stubbed.
"""

import os
import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_effect_veto
from src.mcp_handlers.identity import session as session_mod

# A fixed proposer the minted tokens attest (token `aid` must equal this for §7
# to pass). A different UUID is used for the aid-mismatch case.
PROPOSER = "00000000-0000-0000-0000-0000000000aa"
OTHER_UUID = "00000000-0000-0000-0000-0000000000bb"
TEST_SECRET = "test-continuity-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def _continuity_secret_env():
    """The HMAC secret must be present for BOTH mint and server-side verify —
    `_get_continuity_secret()` reads env on each call. Without it
    `resolve_continuity_token` returns None and every token looks invalid."""
    with patch.dict(os.environ, {"UNITARES_CONTINUITY_TOKEN_SECRET": TEST_SECRET}):
        yield


def _token(aid=PROPOSER, *, mint_at=None):
    """Mint a real continuity token for `aid`. `mint_at` (epoch seconds) lets a
    test mint a token in the past so its `exp` (mint_at + ttl) is already
    elapsed at verify time — exercising the real expiry check."""
    if mint_at is None:
        return session_mod.create_continuity_token(aid, f"sid-{aid[:8]}")
    with patch.object(session_mod.time, "time", return_value=mint_at):
        return session_mod.create_continuity_token(aid, f"sid-{aid[:8]}")


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


class _FakeAcquire:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _FakeConn(self._row)

    async def __aexit__(self, *_a):
        return False


class _FakeDB:
    def __init__(self, row=None, raise_exc=None):
        self._row = row
        self._raise = raise_exc

    def acquire(self):
        if self._raise:
            raise self._raise
        return _FakeAcquire(self._row)


def _client():
    app = Starlette(routes=[Route("/v1/effect-veto", http_effect_veto, methods=["POST"])])
    return TestClient(app)


def _post(row=None, *, raise_exc=None, body=None, token="__valid__"):
    """POST a veto request. By default forwards a VALID strong token for
    PROPOSER so §6 behavior can be tested in isolation. Pass ``token=None`` to
    omit it, or a specific token string to exercise §7 failure modes."""
    if body is None:
        body = {"proposer_agent_uuid": PROPOSER}
        if token == "__valid__":
            token = _token()
        if token is not None:
            body = {**body, "proposer_continuity_token": token}
    with patch("src.db.get_db", return_value=_FakeDB(row, raise_exc)):
        return _client().post("/v1/effect-veto", json=body)


# ── §6 verdict gate — a flagged proposer is blocked (tier satisfied) ──────────

def test_high_risk_verdict_is_vetoed():
    r = _post({"verdict": "high-risk", "action": "approve", "risk_score": 0.9})
    assert r.status_code == 200
    assert r.json()["vetoed"] is True


def test_pause_action_is_vetoed():
    for action in ("risk_pause", "cirs_block", "void_pause", "coherence_pause"):
        r = _post({"verdict": "safe", "action": action, "risk_score": 0.85})
        assert r.json()["vetoed"] is True, f"{action} should veto"


# ── a healthy proposer WITH a strong token is allowed ─────────────────────────

def test_safe_approve_with_strong_token_is_allowed():
    r = _post({"verdict": "safe", "action": "approve", "risk_score": 0.1})
    assert r.status_code == 200
    body = r.json()
    assert body["vetoed"] is False
    assert body["tier"] == "strong"
    assert body["tier_ok"] is True


def test_caution_guide_with_strong_token_is_allowed():
    r = _post({"verdict": "caution", "action": "guide", "risk_score": 0.5})
    assert r.json()["vetoed"] is False


# ── §7 tier gate — THE load-bearing proof: not-strong is blocked ──────────────
# Same clean §6 verdict (safe/approve) in every case below; only the identity
# proof varies. So these prove §7 alone flips the decision — never a no-op.

_CLEAN = {"verdict": "safe", "action": "approve", "risk_score": 0.1}


def test_no_token_is_vetoed_even_with_clean_verdict():
    r = _post(_CLEAN, token=None)
    assert r.status_code == 200
    body = r.json()
    assert body["vetoed"] is True
    assert body["tier_ok"] is False
    assert body["tier"] == "unverified"


def test_malformed_token_is_vetoed():
    r = _post(_CLEAN, token="v1.not-a-real-token.bad-sig")
    assert r.json()["vetoed"] is True
    assert r.json()["tier_ok"] is False


def test_expired_token_is_vetoed():
    # Minted ~2h in the past → exp elapsed; clean §6 verdict cannot save it.
    expired = _token(mint_at=int(time.time()) - 7200)
    r = _post(_CLEAN, token=expired)
    assert r.json()["vetoed"] is True
    assert r.json()["tier_ok"] is False


def test_aid_mismatch_token_is_vetoed():
    # A perfectly valid, fresh token — but for a DIFFERENT identity than the
    # claimed proposer. Closes the confused-deputy path.
    other = _token(aid=OTHER_UUID)
    r = _post(_CLEAN, token=other)
    assert r.json()["vetoed"] is True
    assert r.json()["tier_ok"] is False


def test_secret_unset_fails_closed():
    # If the HMAC secret is missing in the gov-mcp process, NO token can verify
    # → every effect fails closed (loudly blocked), never silently allowed.
    valid = _token()
    with patch.dict(os.environ, {}, clear=True):
        r = _post(_CLEAN, token=valid)
    assert r.json()["vetoed"] is True
    assert r.json()["tier_ok"] is False


# ── policy edges ──────────────────────────────────────────────────────────────

def test_unknown_proposer_with_strong_token_fails_open():
    # §6 fails open for a never-governed proposer — but only when §7 is
    # satisfied. A strong, never-flagged identity may spawn.
    r = _post(row=None)  # no governance state; default token is valid+strong
    assert r.status_code == 200
    body = r.json()
    assert body["vetoed"] is False
    assert body["reason"] == "no_governance_state"
    assert body["tier_ok"] is True


def test_unknown_proposer_without_token_is_vetoed():
    # The hole §6 alone left open: an unknown proposer with no identity proof
    # must NOT slip through. §7 closes it.
    r = _post(row=None, token=None)
    assert r.status_code == 200
    body = r.json()
    assert body["vetoed"] is True
    assert body["tier_ok"] is False


def test_db_error_returns_503_so_caller_fails_closed():
    r = _post(raise_exc=RuntimeError("db down"))
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_missing_proposer_is_422():
    r = _post(body={})
    assert r.status_code == 422
