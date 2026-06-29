"""Proof-tests for the §8 effect-binding gate at POST /v1/effect-veto (#1075).

§8 is ADDITIVE and flag-gated (UNITARES_GOVERNED_EFFECT_BINDING):
  - flag OFF (default) → binding_ok defaults True, the live §6/§7 veto is
    byte-identical (covered by test_http_api_effect_veto.py, which sets no flag);
  - flag ON → the forwarded grant must cover THIS exact effect AND its nonce must
    be unconsumed, else the effect is vetoed.

Proven here so neither half is a silent no-op:
  - valid grant + fresh nonce → allowed (binding_ok True);
  - replayed nonce (INSERT ... ON CONFLICT hit) → vetoed (binding_replayed);
  - no grant → vetoed (binding_absent);
  - grant for a different payload → vetoed (binding_mismatch_psha);
  - nonce-store error → vetoed (binding_store_unavailable), fail-closed.

Governance row is None (unknown proposer → §6 fails open) so §7+§8 are isolated.
Tokens + grants are minted with the real primitives under a test HMAC secret.
"""

import os
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_effect_veto
from src.effect_grant import mint_effect_grant
from src.mcp_handlers.identity import session as session_mod

PROPOSER = "00000000-0000-0000-0000-0000000000aa"
TEST_SECRET = "test-continuity-secret-0123456789abcdef"

_CONTENT = {
    "payload_sha256": "a" * 64,
    "surface": "file://sandbox/x",
    "custody_mode": "execute",
    "idempotency_key": "idem-key-001",
}


@pytest.fixture(autouse=True)
def _env():
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


def _grant(**overrides):
    fields = {**_CONTENT, **overrides}
    return mint_effect_grant(aid=PROPOSER, **fields)


class _FakeConn:
    def __init__(self, row, insert_result, execute_exc):
        self._row = row
        self._insert = insert_result
        self._execute_exc = execute_exc

    async def fetchrow(self, *_a, **_k):
        return self._row

    async def execute(self, *_a, **_k):
        if self._execute_exc:
            raise self._execute_exc
        return self._insert


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _FakeDB:
    def __init__(self, row=None, insert_result="INSERT 0 1", execute_exc=None):
        self._conn = _FakeConn(row, insert_result, execute_exc)

    def acquire(self):
        return _FakeAcquire(self._conn)


def _post(body, *, insert_result="INSERT 0 1", execute_exc=None):
    with patch("src.db.get_db", return_value=_FakeDB(None, insert_result, execute_exc)):
        app = Starlette(routes=[Route("/v1/effect-veto", http_effect_veto, methods=["POST"])])
        return TestClient(app).post("/v1/effect-veto", json=body)


def _body(grant="__valid__", **content_overrides):
    body = {
        "proposer_agent_uuid": PROPOSER,
        "proposer_continuity_token": _token(),
        **_CONTENT,
        **content_overrides,
    }
    if grant == "__valid__":
        grant = _grant()
    if grant is not None:
        body["proposer_effect_grant"] = grant
    return body


# ── allow: valid grant + fresh nonce ─────────────────────────────────────────

def test_valid_grant_fresh_nonce_is_allowed():
    r = _post(_body())
    assert r.status_code == 200
    j = r.json()
    assert j["binding_ok"] is True
    assert j["vetoed"] is False


# ── replay: nonce already consumed ───────────────────────────────────────────

def test_replayed_nonce_is_vetoed():
    r = _post(_body(), insert_result="INSERT 0 0")  # ON CONFLICT hit → 0 rows
    j = r.json()
    assert j["binding_ok"] is False
    assert j["vetoed"] is True
    assert j["reason"] == "binding_replayed"


# ── absent / mismatched / store-error → vetoed (fail-closed) ─────────────────

def test_no_grant_is_vetoed():
    r = _post(_body(grant=None))
    j = r.json()
    assert j["binding_ok"] is False and j["vetoed"] is True
    assert j["reason"] == "binding_absent"


def test_grant_for_different_payload_is_vetoed():
    # grant minted for payload A, effect declares payload B → T1 mismatch
    mismatched = _grant(payload_sha256="b" * 64)
    r = _post(_body(grant=mismatched))
    j = r.json()
    assert j["binding_ok"] is False and j["vetoed"] is True
    assert j["reason"] == "binding_mismatch_psha"


def test_nonce_store_error_fails_closed():
    r = _post(_body(), execute_exc=RuntimeError("db down"))
    j = r.json()
    assert j["binding_ok"] is False and j["vetoed"] is True
    assert j["reason"] == "binding_store_unavailable"


# ── flag off → §8 is a no-op (live veto unchanged) ───────────────────────────

def test_flag_off_is_noop_even_without_grant():
    with patch.dict(os.environ, {"UNITARES_GOVERNED_EFFECT_BINDING": "0"}):
        r = _post(_body(grant=None))
    j = r.json()
    # no grant, but binding disabled → not vetoed on §8 grounds; tier still ok
    assert j["binding_ok"] is True
    assert j["vetoed"] is False
