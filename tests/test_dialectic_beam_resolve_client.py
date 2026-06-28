"""
Tests for the BEAM dialectic-resolve client (dialectic-on-BEAM Slice 1.2).

The client must be fail-safe: it returns None (caller falls back to the Python
pg_resolve_session path) whenever the flag is off, the lease plane is not
configured, a required field is missing, or BEAM returns non-OK — and must never
raise.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.dialectic import beam_resolve_client as brc


def _fake_httpx(status_code, body):
    """Build a fake httpx module whose Client().post() returns the given response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x"
    resp.json.return_value = body
    client_cm = MagicMock()
    client_cm.__enter__.return_value.post.return_value = resp
    client_cm.__exit__.return_value = False
    fake = MagicMock()
    fake.Client.return_value = client_cm
    return fake


@pytest.mark.asyncio
async def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
    out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_enabled_but_no_bearer_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_enabled_missing_reviewer_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    out = await brc.beam_resolve("s1", "p", None, {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_success_returns_body(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "true")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": True, "status": "resolved", "saga_id": "abc", "origin": "new"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}):
        out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out == body


@pytest.mark.asyncio
async def test_saga_in_flight_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "saga_in_flight"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_transport_error_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    boom = MagicMock()
    boom.Client.side_effect = RuntimeError("connection refused")
    with patch.dict(sys.modules, {"httpx": boom}):
        out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None
