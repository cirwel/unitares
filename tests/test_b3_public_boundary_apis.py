"""Unit tests for the public cross-component APIs exposed to clear B3 private imports."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src import effect_grant, trajectory_identity
from src.http_routes import access
from src.identity import agent_identity_credential as aic
from src.mcp_handlers.identity import handlers as id_handlers
from src.mcp_handlers.identity import resolution as id_resolution
from src.mcp_handlers.identity import session as id_session
from src.mcp_handlers.identity import shared as id_shared
from src.mcp_handlers.middleware import identity_step, params_step


def test_b64u_roundtrip_matches_private_helpers():
    raw = b"\x00\x01\x02dialectic-receipt-payload\xff"
    encoded = aic.b64u_encode(raw)
    assert encoded == aic._b64u_encode(raw)
    assert aic.b64u_decode(encoded) == raw
    assert aic.b64u_decode(encoded) == aic._b64u_decode(encoded)


def test_continuity_secret_delegates_through_private_hook(monkeypatch):
    monkeypatch.setattr(id_session, "_get_continuity_secret", lambda: b"s3cr3t")
    assert id_session.get_continuity_secret() == b"s3cr3t"
    assert effect_grant._get_continuity_secret() == b"s3cr3t"


def test_extract_base_fingerprint_matches_private():
    key = "160.79.106.108:d20c2f:suffix"
    assert id_session.extract_base_fingerprint(key) == "ua:d20c2f"
    assert id_session.extract_base_fingerprint(key) == id_session._extract_base_fingerprint(key)


def test_explicit_target_calls_alias():
    assert params_step.EXPLICIT_TARGET_CALLS is params_step._EXPLICIT_TARGET_CALLS
    assert params_step.EXPLICIT_TARGET_CALLS.matches("agent", {"action": "delete"})
    assert params_step.EXPLICIT_TARGET_CALLS.matches("agent", {"action": "archive"})
    assert not params_step.EXPLICIT_TARGET_CALLS.matches("agent", {"action": "get"})


def test_dtw_similarity_matches_private():
    s1 = [0.1, 0.2, 0.3, 0.4, 0.5]
    s2 = [0.1, 0.25, 0.3, 0.4, 0.5]
    assert trajectory_identity.dtw_similarity(s1, s2) == trajectory_identity._dtw_similarity(s1, s2)
    assert trajectory_identity.dtw_similarity([], s2) == 0.0


def test_identity_shared_accessors_reflect_live_globals(monkeypatch):
    fake_sessions = {"k1": {"bound_agent_id": "a1"}}
    fake_prefixes = {"deadbeef1234": "deadbeef-1234-5678-90ab-cdef12345678"}
    monkeypatch.setattr(id_shared, "_session_identities", fake_sessions)
    monkeypatch.setattr(id_shared, "_uuid_prefix_index", fake_prefixes)
    monkeypatch.setattr(id_shared, "_get_lineage", lambda aid: ["root", aid])

    assert id_shared.get_session_identities() is fake_sessions
    assert id_shared.get_uuid_prefix_index() is fake_prefixes
    assert id_shared.get_lineage("child") == ["root", "child"]


def test_derive_onboard_response_mode_and_broadcaster(monkeypatch):
    assert id_handlers.derive_onboard_response_mode({}) == id_handlers._derive_onboard_response_mode({})
    assert id_handlers.derive_onboard_response_mode({"response_mode": "full"}) == (False, "full")

    sentinel = object()
    monkeypatch.setattr(id_handlers, "_broadcaster", lambda: sentinel)
    assert id_handlers.get_broadcaster() is sentinel


@pytest.mark.asyncio
async def test_identity_async_wrappers_delegate_for_patching(monkeypatch):
    exists_mock = AsyncMock(return_value=True)
    status_mock = AsyncMock(return_value="active")
    cache_mock = AsyncMock(return_value=None)
    label_mock = AsyncMock(return_value="uuid-1")
    reject_mock = AsyncMock(return_value={"resume_failed": True})

    monkeypatch.setattr(id_handlers, "_agent_exists_in_postgres", exists_mock)
    monkeypatch.setattr(id_handlers, "_get_agent_status", status_mock)
    monkeypatch.setattr(id_handlers, "_cache_session", cache_mock)
    monkeypatch.setattr(id_handlers, "_find_agent_by_label", label_mock)
    monkeypatch.setattr(id_resolution, "_substrate_http_reject", reject_mock)

    assert await id_handlers.agent_exists_in_postgres("uuid-1") is True
    exists_mock.assert_awaited_once_with("uuid-1")

    assert await id_handlers.get_agent_status("uuid-1") == "active"
    status_mock.assert_awaited_once_with("uuid-1")

    await id_handlers.cache_session("sk", "uuid-1")
    cache_mock.assert_awaited_once_with("sk", "uuid-1")

    assert await id_handlers.find_agent_by_label("Lumen") == "uuid-1"
    label_mock.assert_awaited_once_with("Lumen")

    res = await id_resolution.substrate_http_reject("uuid-1", source="unit")
    assert res == {"resume_failed": True}
    reject_mock.assert_awaited_once_with("uuid-1", source="unit")


def test_transport_cache_key_and_looks_like_uuid(monkeypatch):
    monkeypatch.setattr(identity_step, "_transport_cache_key", lambda sig: f"sticky:{sig}")
    assert identity_step.transport_cache_key("fp") == "sticky:fp"

    assert access.looks_like_uuid("11111111-2222-3333-4444-555555555555") is True
    assert access.looks_like_uuid("not-a-uuid") is False
    assert access.looks_like_uuid(None) is False
