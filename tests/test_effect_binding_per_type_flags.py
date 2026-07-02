"""Per-effect-type binding flags (#1252 item 2).

The global ``UNITARES_GOVERNED_EFFECT_BINDING`` enforces every effect type at
once; per-type flags (``UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE``, …,
derived generically from the forwarded ``effect_type``) stage the rollout so
``file_write`` can be enforced and proven while ``agent_spawn``'s ad-hoc
producers stay unbound. Grant *minting* opens when the global OR any per-type
flag is set — producers must be able to mint before every type enforces.

Proven here so the staging is not a silent no-op in either direction:
  - per-type flag on + matching effect_type + no grant → vetoed (enforced);
  - per-type flag on + OTHER effect_type → allowed grantless (not enforced);
  - per-type flag on + missing effect_type → not enforced (global remains the
    blanket lockdown);
  - global flag on → enforced regardless of type (back-compat);
  - mint endpoint opens under a per-type flag alone, stays 501 with none.
"""

import os
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import (
    _binding_enforced,
    _binding_mint_enabled,
    http_effect_grant,
    http_effect_veto,
)
from src.mcp_handlers.identity import session as session_mod

PROPOSER = "00000000-0000-0000-0000-0000000000bb"
TEST_SECRET = "test-continuity-secret-0123456789abcdef"

_BASE_ENV = {
    "UNITARES_CONTINUITY_TOKEN_SECRET": TEST_SECRET,
    "UNITARES_MCP_BEARER_TOKENS": "",
    "UNITARES_HTTP_API_TOKEN": "",
    "UNITARES_GOVERNED_EFFECT_BINDING": "",
    "UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE": "",
    "UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN": "",
}


def _env(**flags):
    return patch.dict(os.environ, {**_BASE_ENV, **flags})


class _FakeConn:
    async def fetchrow(self, *_a, **_k):
        return None

    async def execute(self, *_a, **_k):
        return "INSERT 0 1"


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *_a):
        return False


class _FakeDB:
    def acquire(self):
        return _FakeAcquire()


def _post_veto(body):
    with patch("src.db.get_db", return_value=_FakeDB()):
        app = Starlette(routes=[Route("/v1/effect-veto", http_effect_veto, methods=["POST"])])
        return TestClient(app).post("/v1/effect-veto", json=body)


def _post_grant(body):
    app = Starlette(routes=[Route("/v1/effect-grant", http_effect_grant, methods=["POST"])])
    return TestClient(app).post("/v1/effect-grant", json=body)


def _veto_body(effect_type):
    body = {
        "proposer_agent_uuid": PROPOSER,
        "proposer_continuity_token": session_mod.create_continuity_token(
            PROPOSER, f"sid-{PROPOSER[:8]}"
        ),
        "surface": "file://sandbox/x",
        "custody_mode": "execute",
        "idempotency_key": "idem-key-per-type",
        "payload_sha256": "a" * 64,
    }
    if effect_type is not None:
        body["effect_type"] = effect_type
    return body


# ── the flag helpers ─────────────────────────────────────────────────────────


def test_binding_enforced_matrix():
    with _env():
        assert _binding_enforced("file_write") is False
    with _env(UNITARES_GOVERNED_EFFECT_BINDING="1"):
        assert _binding_enforced("file_write") is True
        assert _binding_enforced("agent_spawn") is True
        assert _binding_enforced(None) is True
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        assert _binding_enforced("file_write") is True
        assert _binding_enforced("agent_spawn") is False
        assert _binding_enforced(None) is False
        assert _binding_enforced("") is False


def test_binding_mint_enabled_matrix():
    with _env():
        assert _binding_mint_enabled() is False
    with _env(UNITARES_GOVERNED_EFFECT_BINDING="1"):
        assert _binding_mint_enabled() is True
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        assert _binding_mint_enabled() is True
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN="1"):
        assert _binding_mint_enabled() is True


# ── veto: per-type enforcement ───────────────────────────────────────────────


def test_per_type_flag_enforces_matching_type():
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        r = _post_veto(_veto_body("file_write"))
    assert r.status_code == 200
    j = r.json()
    assert j["binding_ok"] is False
    assert j["vetoed"] is True
    assert "binding_absent" in (j.get("reason") or "")


def test_per_type_flag_does_not_enforce_other_type():
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        r = _post_veto(_veto_body("agent_spawn"))
    assert r.status_code == 200
    j = r.json()
    assert j["binding_ok"] is True
    assert j["vetoed"] is False


def test_per_type_flag_missing_effect_type_not_enforced():
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        r = _post_veto(_veto_body(None))
    assert r.status_code == 200
    assert r.json()["binding_ok"] is True


def test_global_flag_enforces_every_type():
    with _env(UNITARES_GOVERNED_EFFECT_BINDING="1"):
        for effect_type in ("file_write", "agent_spawn", None):
            r = _post_veto(_veto_body(effect_type))
            assert r.status_code == 200
            assert r.json()["binding_ok"] is False, f"type={effect_type}"


# ── mint endpoint gating ─────────────────────────────────────────────────────


def _grant_request():
    return {
        "proposer_agent_uuid": PROPOSER,
        "proposer_continuity_token": session_mod.create_continuity_token(
            PROPOSER, f"sid-{PROPOSER[:8]}"
        ),
        "payload_sha256": "a" * 64,
        "surface": "file://sandbox/x",
        "custody_mode": "execute",
        "idempotency_key": "idem-key-mint",
    }


def test_mint_501_with_no_binding_flags():
    with _env():
        r = _post_grant(_grant_request())
    assert r.status_code == 501
    assert r.json()["error"] == "binding_not_enabled"


def test_mint_opens_under_per_type_flag_alone():
    with _env(UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE="1"):
        r = _post_grant(_grant_request())
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["grant"].startswith("gnt.v1.")
