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

    data, audit = await _resume_fastpath(_mint())

    assert data.get("success") is True
    block = data["continuity_token_freshness"]
    assert block == {"expired": False, "token_age_seconds": block["token_age_seconds"]}
    assert audit.call_args.kwargs["expired"] is False


@pytest.mark.asyncio
async def test_path0_without_token_has_no_freshness_block(monkeypatch):
    """A bare-uuid resume (allowed only outside strict) proves nothing by token."""
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "off")

    data, audit = await _resume_fastpath(None)

    assert data.get("success") is True
    assert "continuity_token_freshness" not in data
    audit.assert_not_called()
