"""PATH 0 token-age observation: surface and audit, never change acceptance.

PATH 0 accepts an expired-but-signed continuity_token as ownership proof
(PR #42). These tests pin that this PR only *observes* that: the resume still
succeeds, the caller sees ``continuity_token_freshness``, and one
``path0_token_accept_observed`` audit entry records the token's age.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_UUID = "eeeeeeee-1111-2222-3333-444444444444"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")


def _mint(*, issued_at: int | None = None) -> str:
    from src.mcp_handlers.identity import session

    if issued_at is None:
        return session.create_continuity_token(_UUID, "agent-eeeeeeee-111")
    with patch.object(session.time, "time", return_value=float(issued_at)):
        return session.create_continuity_token(_UUID, "agent-eeeeeeee-111")


def test_freshness_reports_fresh_token():
    from src.mcp_handlers.identity.session import continuity_token_freshness

    fresh = continuity_token_freshness(_mint())
    assert fresh is not None
    assert fresh["expired"] is False
    assert fresh["seconds_past_exp"] is None
    assert fresh["token_exp"] > fresh["token_iat"]


def test_freshness_reports_expired_token_with_skew_tolerance():
    from src.mcp_handlers.identity.session import (
        _CLOCK_SKEW_TOLERANCE,
        continuity_token_freshness,
    )

    token = _mint()
    exp = continuity_token_freshness(token)["token_exp"]
    inside_skew = continuity_token_freshness(token, now=exp + _CLOCK_SKEW_TOLERANCE)
    assert inside_skew["expired"] is False
    past = continuity_token_freshness(token, now=exp + 7200)
    assert past["expired"] is True
    assert past["seconds_past_exp"] == 7200


def _sign(payload: dict) -> str:
    import hashlib
    import hmac

    from src.mcp_handlers.identity import session

    payload_b64 = session._b64url_encode(json.dumps(payload).encode())
    sig = hmac.new(session._get_continuity_secret(), payload_b64.encode(), hashlib.sha256).digest()
    return f"v1.{payload_b64}.{session._b64url_encode(sig)}"


_NOW = int(time.time())


@pytest.mark.parametrize(
    "exp",
    [
        None,
        "soon",
        1e309,
        float("nan"),
        # Finite but absurd: int(1e308) is a giant int that never expires.
        1e308,
        10**400,
        # Not an integer: int() would truncate these into a readable exp.
        float(_NOW + 600),
        _NOW + 600.5,
        True,
        # An integer, but further ahead than any token the server mints.
        _NOW + 3600 + 30 + 1,
        _NOW + 30 * 86400,
    ],
    ids=lambda v: repr(v)[:24],
)
def test_freshness_treats_missing_or_unreadable_exp_as_expired(exp):
    """resolve_continuity_token refuses these, so the observation must agree."""
    from src.mcp_handlers.identity import session
    from src.mcp_handlers.identity.session import (
        continuity_token_freshness,
        resolve_continuity_token,
    )

    payload = {"sid": "agent-eeeeeeee-111", "aid": _UUID, "iat": _NOW}
    if exp is not None:
        payload["exp"] = exp
    token = _sign(payload)
    with patch.object(session.time, "time", return_value=float(_NOW)):
        assert resolve_continuity_token(token) is None
    fresh = continuity_token_freshness(token, now=_NOW)
    assert fresh["expired"] is True
    assert fresh["token_exp"] is None
    assert fresh["seconds_past_exp"] is None


def test_exp_bound_admits_every_token_the_server_mints():
    """The upper bound is TTL + skew past now: the edge is readable, one past is not."""
    from src.mcp_handlers.identity import session
    from src.mcp_handlers.identity.session import (
        _CLOCK_SKEW_TOLERANCE,
        _CONTINUITY_TTL,
        continuity_token_freshness,
        resolve_continuity_token,
    )

    edge = _NOW + _CONTINUITY_TTL + _CLOCK_SKEW_TOLERANCE
    for exp, readable in ((edge, True), (edge + 1, False)):
        token = _sign({"sid": "agent-eeeeeeee-111", "aid": _UUID, "iat": _NOW, "exp": exp})
        with patch.object(session.time, "time", return_value=float(_NOW)):
            resolved = resolve_continuity_token(token)
        assert (resolved == "agent-eeeeeeee-111") is readable
        fresh = continuity_token_freshness(token, now=_NOW)
        assert fresh["expired"] is (not readable)
        assert fresh["token_exp"] == (exp if readable else None)


def test_mint_clamps_ttl_to_the_bound():
    """A longer ttl_seconds cannot mint a token the resolver would then refuse."""
    from src.mcp_handlers.identity import session

    with patch.object(session.time, "time", return_value=float(_NOW)):
        token = session.create_continuity_token(
            _UUID, "agent-eeeeeeee-111", ttl_seconds=30 * 86400
        )
        assert session.resolve_continuity_token(token) == "agent-eeeeeeee-111"
    assert session.extract_token_exp(token) == _NOW + session._CONTINUITY_TTL


@pytest.mark.parametrize("claim", ["iat", "exp"])
@pytest.mark.parametrize("value", [1e309, -1e309, float("nan"), "soon", 1.5, True])
def test_claim_accessors_return_none_for_non_finite_or_malformed(claim, value):
    """extract_token_iat / extract_token_exp feed observation callers, which
    must get None for an unreadable claim rather than an OverflowError."""
    from src.mcp_handlers.identity.session import extract_token_exp, extract_token_iat

    now = int(time.time())
    payload = {"sid": "agent-eeeeeeee-111", "aid": _UUID, "iat": now, "exp": now + 3600}
    payload[claim] = value
    token = _sign(payload)
    accessor = extract_token_iat if claim == "iat" else extract_token_exp
    assert accessor(token) is None


def test_freshness_rejects_unverified_token():
    from src.mcp_handlers.identity.session import continuity_token_freshness

    token = _mint()
    head, payload, _sig = token.split(".", 2)
    assert continuity_token_freshness(f"{head}.{payload}.bad") is None
    assert continuity_token_freshness("") is None


def test_extract_token_agent_uuid_still_ignores_expiry():
    """The PR #42 contract is untouched: an expired token still yields its uuid."""
    from src.mcp_handlers.identity.session import extract_token_agent_uuid

    old = _mint(issued_at=int(time.time()) - 10 * 86400)
    assert extract_token_agent_uuid(old) == _UUID


async def _resume_fastpath(token: str | None):
    from src.mcp_handlers.identity.handlers import handle_identity_adapter

    fake_server = MagicMock(monitors={_UUID: object()}, agent_metadata={})
    args = {"agent_uuid": _UUID, "resume": True}
    if token:
        args["continuity_token"] = token
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server), \
         patch(
             "src.mcp_handlers.identity.handlers._schedule_path0_audit_write",
             side_effect=lambda write: write(),
         ), \
         patch("src.audit_log.audit_logger.log_path0_token_accept_observed") as audit:
        result = await handle_identity_adapter(args)
    return json.loads(result[0].text), audit


@pytest.mark.asyncio
async def test_path0_expired_token_still_resumes_and_is_surfaced(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    old = _mint(issued_at=int(time.time()) - 3 * 3600)

    data, audit = await _resume_fastpath(old)

    assert data.get("success") is True
    assert data.get("resumed") is True
    block = data["continuity_token_freshness"]
    assert block["expired"] is True
    assert block["seconds_past_exp"] >= 2 * 3600 - 60
    assert "fresh continuity_token" in block["note"]
    # The fresh token the note points at is actually in the response.
    assert data.get("continuity_token") and data["continuity_token"] != old
    audit.assert_called_once()
    kwargs = audit.call_args.kwargs
    assert kwargs["agent_uuid"] == _UUID
    assert kwargs["resume_source"] == "monitor_cache"
    assert kwargs["expired"] is True


@pytest.mark.asyncio
async def test_path0_fresh_token_surfaces_not_expired(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")

    issued_at = int(time.time()) - 120
    data, audit = await _resume_fastpath(_mint(issued_at=issued_at))
    elapsed = int(time.time()) - issued_at

    assert data.get("success") is True
    block = data["continuity_token_freshness"]
    assert set(block) == {"expired", "token_age_seconds"}
    assert block["expired"] is False
    assert 120 <= block["token_age_seconds"] <= elapsed
    assert audit.call_args.kwargs["expired"] is False
    assert audit.call_args.kwargs["token_age_seconds"] == block["token_age_seconds"]


@pytest.mark.asyncio
async def test_path0_without_token_has_no_freshness_block(monkeypatch):
    """A bare-uuid resume (allowed only outside strict) proves nothing by token."""
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "off")

    data, audit = await _resume_fastpath(None)

    assert data.get("success") is True
    assert "continuity_token_freshness" not in data
    audit.assert_not_called()


@pytest.mark.asyncio
async def test_audit_write_is_scheduled_off_the_request_thread():
    """The flock/fsync append must not run inline on the event loop."""
    import asyncio
    import threading
    from src.mcp_handlers.identity.handlers import _schedule_path0_audit_write

    ran_on = []
    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _write():
        ran_on.append(threading.get_ident())
        loop.call_soon_threadsafe(done.set)

    _schedule_path0_audit_write(_write)
    await asyncio.wait_for(done.wait(), timeout=5)
    assert ran_on and ran_on[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_executor_refusal_does_not_change_acceptance(monkeypatch):
    """A refused executor submission drops the observation, never the resume."""
    import asyncio
    from src.mcp_handlers.identity.handlers import handle_identity_adapter

    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    loop = asyncio.get_running_loop()

    def _refuse(*_a, **_k):
        raise RuntimeError("cannot schedule new futures after shutdown")

    fake_server = MagicMock(monitors={_UUID: object()}, agent_metadata={})
    with patch.object(loop, "run_in_executor", side_effect=_refuse), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server):
        result = await handle_identity_adapter(
            {"agent_uuid": _UUID, "resume": True, "continuity_token": _mint()}
        )
    data = json.loads(result[0].text)
    assert data.get("success") is True
    assert data.get("source") == "monitor_cache"
    assert data["continuity_token_freshness"]["expired"] is False


def test_observation_row_carries_no_confidence(tmp_path):
    """confidence=0.0 keeps this row out of get_latest_confidence_before."""
    from src import audit_log as audit_mod

    captured = []
    logger_obj = audit_mod.audit_logger
    with patch.object(logger_obj, "_write_entry", side_effect=captured.append):
        logger_obj.log_path0_token_accept_observed(
            agent_uuid=_UUID, resume_source="db", token_iat=1, token_exp=2,
            token_age_seconds=3, expired=True, seconds_past_exp=4,
        )
    assert captured and captured[0].confidence == 0.0
    assert captured[0].event_type == "path0_token_accept_observed"
