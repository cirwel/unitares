"""Proof-tests for POST /v1/effect-veto (governed-effect §6 governance veto).

The gate that matters: a FLAGGED proposer provably gets vetoed, so the veto can
never be a silent no-op. Governance state is mocked (deterministic, no DB
pollution) — the handler's verdict/action extraction is what's under test.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_effect_veto


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


def _post(row=None, *, raise_exc=None, body=None):
    body = body if body is not None else {"proposer_agent_uuid": "00000000-0000-0000-0000-0000000000aa"}
    with patch("src.db.get_db", return_value=_FakeDB(row, raise_exc)):
        return _client().post("/v1/effect-veto", json=body)


# --- THE load-bearing proof: a flagged proposer is blocked ---

def test_high_risk_verdict_is_vetoed():
    r = _post({"verdict": "high-risk", "action": "approve", "risk_score": 0.9})
    assert r.status_code == 200
    assert r.json()["vetoed"] is True


def test_pause_action_is_vetoed():
    for action in ("risk_pause", "cirs_block", "void_pause", "coherence_pause"):
        r = _post({"verdict": "safe", "action": action, "risk_score": 0.85})
        assert r.json()["vetoed"] is True, f"{action} should veto"


# --- a healthy proposer is allowed ---

def test_safe_approve_is_allowed():
    r = _post({"verdict": "safe", "action": "approve", "risk_score": 0.1})
    assert r.status_code == 200
    assert r.json()["vetoed"] is False


def test_caution_guide_is_allowed():
    r = _post({"verdict": "caution", "action": "guide", "risk_score": 0.5})
    assert r.json()["vetoed"] is False


# --- policy edges ---

def test_unknown_proposer_fails_open():
    r = _post(row=None)  # no governance state
    assert r.status_code == 200
    body = r.json()
    assert body["vetoed"] is False
    assert body["reason"] == "no_governance_state"


def test_db_error_returns_503_so_caller_fails_closed():
    r = _post(raise_exc=RuntimeError("db down"))
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_missing_proposer_is_422():
    r = _post(body={})
    assert r.status_code == 422
