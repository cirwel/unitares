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

import asyncio
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
        return TestClient(app, client=("127.0.0.1", 50000)).post(
            "/v1/effect-veto", json=body
        )


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


# ── nonce-ledger purge: bounded, rate-limited, never changes the outcome ─────

class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.statements.append("BEGIN")
        return self

    async def __aexit__(self, exc_type, *_a):
        self._conn.statements.append("ROLLBACK" if exc_type else "COMMIT")
        return False


class _RecordingConn(_FakeConn):
    """Consume succeeds; the purge DELETE can raise or block until released."""

    def __init__(self, purge_exc=None, purge_gate=None):
        super().__init__(None, "INSERT 0 1", None)
        self.statements = []
        self._purge_exc = purge_exc
        self._purge_gate = purge_gate
        self.purge_started = asyncio.Event()

    def transaction(self):
        return _FakeTxn(self)

    async def execute(self, sql, *args, **_k):
        self.statements.append(sql)
        if "DELETE" in sql:
            self.purge_started.set()
            if self._purge_gate is not None:
                await self._purge_gate.wait()
            if self._purge_exc:
                raise self._purge_exc
        return "INSERT 0 1"


def _recording_db(conn):
    db = _FakeDB()
    db._conn = conn
    return db


@pytest.fixture
def _purge_due(monkeypatch):
    from src.http_routes import effects as effects_mod

    monkeypatch.setattr(effects_mod, "_last_nonce_purge", 0.0)
    monkeypatch.setattr(effects_mod, "_nonce_purge_task", None)
    return effects_mod


async def _check(conn):
    from src.http_routes.effects import _check_effect_binding

    with patch("src.db.get_db", return_value=_recording_db(conn)):
        return await _check_effect_binding(_body(), PROPOSER)


async def _drain(effects_mod):
    task = effects_mod._nonce_purge_task
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_purge_runs_after_consume_and_is_rate_limited(_purge_due):
    conn = _RecordingConn()
    assert await _check(conn) == (True, None)
    await _drain(_purge_due)
    assert await _check(conn) == (True, None)
    await _drain(_purge_due)
    deletes = [s for s in conn.statements if "DELETE" in s]
    assert len(deletes) == 1
    assert "LIMIT" in deletes[0]


@pytest.mark.asyncio
async def test_purge_is_bounded_and_skips_locked_rows(_purge_due):
    conn = _RecordingConn()
    assert await _check(conn) == (True, None)
    await _drain(_purge_due)
    purge = conn.statements[conn.statements.index("BEGIN"):]
    assert purge[-1] == "COMMIT"
    assert any("SET LOCAL lock_timeout" in s for s in purge)
    assert any("SET LOCAL statement_timeout" in s for s in purge)
    delete = next(s for s in purge if "DELETE" in s)
    assert "FOR UPDATE SKIP LOCKED" in delete


@pytest.mark.asyncio
async def test_purge_failure_does_not_veto(_purge_due):
    conn = _RecordingConn(purge_exc=RuntimeError("purge failed"))
    assert await _check(conn) == (True, None)
    await _drain(_purge_due)  # the failure is logged inside the task, not raised


@pytest.mark.asyncio
async def test_blocked_purge_does_not_delay_the_veto(_purge_due):
    """A purge stuck on a lock must not hold the allow decision: the lease plane
    times out its veto call, and the nonce is already consumed, so a delayed
    allow would become a veto the proposer cannot retry."""
    gate = asyncio.Event()
    conn = _RecordingConn(purge_gate=gate)
    try:
        result = await asyncio.wait_for(_check(conn), timeout=1.0)
        assert result == (True, None)
        await asyncio.wait_for(conn.purge_started.wait(), timeout=1.0)
        # A second consume while the first purge is still in flight starts no
        # second purge, even once the interval has elapsed.
        _purge_due._last_nonce_purge = 0.0
        assert await asyncio.wait_for(_check(conn), timeout=1.0) == (True, None)
        assert sum("DELETE" in s for s in conn.statements) == 1
    finally:
        gate.set()
        await _drain(_purge_due)


@pytest.mark.asyncio
async def test_hung_purge_is_cut_off_by_its_deadline(_purge_due, monkeypatch):
    monkeypatch.setattr(_purge_due, "_NONCE_PURGE_DEADLINE_SECONDS", 0.05)
    conn = _RecordingConn(purge_gate=asyncio.Event())  # never released
    assert await _check(conn) == (True, None)
    await asyncio.wait_for(_purge_due._nonce_purge_task, timeout=2.0)
    assert _purge_due._nonce_purge_task.done()
    assert conn.statements[-1] == "ROLLBACK"


@pytest.mark.asyncio
async def test_purge_sql_keeps_live_nonces_against_real_db():
    """Expired-past-margin and unmintable far-future rows go; live rows stay,
    a row another transaction has locked is skipped without waiting, and a
    replay of a live nonce is still refused after the purge."""
    from tests.test_db_utils import (
        TEST_DB_URL,
        can_connect_to_test_db,
        ensure_test_database_schema,
    )

    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")

    import asyncpg

    from src.http_routes.effects import _purge_consumed_nonces

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    prefix = "purge-test-"
    try:
        exists = await conn.fetchval("SELECT to_regclass('effects.consumed_nonces')")
        if exists is None:
            pytest.skip("effects.consumed_nonces not present in governance_test")
        await conn.execute(
            "DELETE FROM effects.consumed_nonces WHERE nonce LIKE $1", prefix + "%"
        )
        rows = {
            "old": "now() - interval '10 minutes'",
            "old-locked": "now() - interval '10 minutes'",
            "just-expired": "now() - interval '5 seconds'",
            "live": "now() + interval '120 seconds'",
            "far-future": "now() + interval '3650 days'",
        }
        for name, exp_sql in rows.items():
            await conn.execute(
                f"INSERT INTO effects.consumed_nonces (nonce, grant_exp) "
                f"VALUES ($1, {exp_sql})",
                prefix + name,
            )

        # Another transaction holds a row lock on one purgeable row: the purge
        # skips it rather than waiting on it.
        locker = await asyncpg.connect(TEST_DB_URL)
        locker_txn = locker.transaction()
        await locker_txn.start()
        try:
            await locker.fetchval(
                "SELECT nonce FROM effects.consumed_nonces WHERE nonce = $1 FOR UPDATE",
                prefix + "old-locked",
            )
            await asyncio.wait_for(_purge_consumed_nonces(conn), timeout=3.0)
        finally:
            await locker_txn.rollback()
            await locker.close()

        left = {
            r["nonce"][len(prefix):]
            for r in await conn.fetch(
                "SELECT nonce FROM effects.consumed_nonces WHERE nonce LIKE $1",
                prefix + "%",
            )
        }
        assert left == {"just-expired", "live", "old-locked"}

        replay = await conn.execute(
            "INSERT INTO effects.consumed_nonces (nonce, grant_exp) "
            "VALUES ($1, now() + interval '120 seconds') ON CONFLICT (nonce) DO NOTHING",
            prefix + "live",
        )
        assert replay.split()[-1] == "0"
    finally:
        await conn.execute(
            "DELETE FROM effects.consumed_nonces WHERE nonce LIKE $1", prefix + "%"
        )
        await conn.close()
