"""PATH 1 fingerprint cross-check — Phase A (observation).

Council follow-up to identity-honesty Part C (KG 2026-04-20T00:57:45).
`client_session_id` values of shape `agent-{uuid[:12]}` are UUID-derivable,
so PATH 1 resume by session_id alone has no ownership proof. This module
locks in the observation-phase behavior of the fingerprint cross-check
added at the PATH 1 cache-hit site:

  - `_cache_session` writes `bind_ip_ua` alongside the existing fields
    when SessionSignals carry a fingerprint.
  - `resolve_session_identity` PATH 1 reads `bind_ip_ua`; on mismatch with
    the current request's fingerprint, fires `identity_hijack_suspected`
    with `path="path1_session_id"`.
  - In `log` mode the resume still proceeds after emission.
  - In `strict` mode (default), mismatched resumes fall through to a fresh session.
  - In `off` mode, no check runs — no event, no fall-through.
  - Legacy cache entries without `bind_ip_ua` are treated as unknown
    (no event — preserves backward compat).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_fingerprint_mode(monkeypatch):
    monkeypatch.delenv("UNITARES_SESSION_FINGERPRINT_CHECK", raising=False)
    yield


def _signals_with_fp(fp: str):
    sig = MagicMock()
    sig.ip_ua_fingerprint = fp
    return sig


class TestPath1FingerprintMismatchEmits:
    """PATH 1 cache-hit with divergent fingerprint emits identity_hijack_suspected."""

    @pytest.fixture
    def captured_events(self):
        return []

    @pytest.fixture
    def broadcaster_stub(self, captured_events):
        b = MagicMock()

        async def _record(**kwargs):
            captured_events.append(kwargs)

        b.broadcast_event = AsyncMock(side_effect=_record)
        return b

    @pytest.mark.asyncio
    async def test_log_mode_emits_and_proceeds(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "log")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "11111111-2222-3333-4444-555555555555"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_Code_20260420",
            "bind_ip_ua": "ip-1.2.3.4:ua-aaaaaaaaaaaa",
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_Code_20260420"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-9.9.9.9:ua-bbbbbbbbbbbb"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-111111111222",
                resume=True,
            )

        # Log mode: resume still succeeds.
        assert result.get("source") == "redis", (
            f"Log mode must preserve the cached-resume outcome. Got: {result}"
        )
        assert result.get("agent_uuid") == agent_uuid

        # And event fires.
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events, (
            f"Mismatch must emit identity_hijack_suspected. Captured: {captured_events}"
        )
        evt = hijack_events[0]
        assert evt.get("agent_id") == agent_uuid
        payload = evt.get("payload") or {}
        assert payload.get("path") == "path1_session_id"
        assert payload.get("mode") == "log"

    @pytest.mark.asyncio
    async def test_strict_mode_falls_through(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "strict")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "22222222-3333-4444-5555-666666666666"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_Code_strict",
            "bind_ip_ua": "ip-original:ua-original",
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-attacker:ua-attacker"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ), patch.object(res, "_agent_exists_in_postgres", new=AsyncMock(return_value=False)), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            side_effect=RuntimeError("PATH 2 shouldn't be reached successfully"),
        ), patch(
            "src.mcp_handlers.identity.resolution._generate_agent_id",
            return_value="Claude_fresh",
        ):
            result = await res.resolve_session_identity(
                session_key="agent-222222222333",
                resume=True,
                persist=False,
            )

        # Strict mode: resume must NOT return the cached UUID.
        assert result.get("agent_uuid") != agent_uuid, (
            f"Strict mode must refuse to reuse the cached UUID on fingerprint "
            f"mismatch. Got agent_uuid={result.get('agent_uuid')}, source={result.get('source')}"
        )

        # Event still fires.
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events
        assert (hijack_events[0].get("payload") or {}).get("mode") == "strict"

    @pytest.mark.asyncio
    async def test_matching_fingerprint_no_event(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "log")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "33333333-4444-5555-6666-777777777777"
        fp = "ip-same:ua-same"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_Code_match",
            "bind_ip_ua": fp,
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_Code_match"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp(fp),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-333333333444",
                resume=True,
            )

        assert result.get("agent_uuid") == agent_uuid
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events == [], (
            "Matching fingerprint is the expected resume shape — no event"
        )

    @pytest.mark.asyncio
    async def test_legacy_cache_without_bind_fp_no_event(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """Cache entries written before this feature lack `bind_ip_ua`. They
        must be treated as unknown — not suspicious. Otherwise strict-mode
        promotion would retroactively invalidate every pre-existing session."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "log")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "44444444-5555-6666-7777-888888888888"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_legacy",
            # no bind_ip_ua field
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_legacy"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-any:ua-any"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-444444444555",
                resume=True,
            )

        assert result.get("agent_uuid") == agent_uuid
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events == [], (
            "Legacy entry without bind_ip_ua must pass silently"
        )

    @pytest.mark.asyncio
    async def test_off_mode_skips_check(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "55555555-6666-7777-8888-999999999999"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_off",
            "bind_ip_ua": "ip-original:ua-original",
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_off"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-different:ua-different"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-555555555666",
                resume=True,
            )

        # Off mode: resume succeeds AND no event fires.
        assert result.get("agent_uuid") == agent_uuid
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events == [], (
            "Off mode is explicit opt-out — no event even on mismatch"
        )


class TestPath1TokenOwnershipCrossCheck:
    """PATH 1 cache-hit with divergent token_agent_uuid falls through unconditionally.

    Unlike fingerprint (soft heuristic, operator-configurable), token-uuid
    mismatch is cryptographic proof of different ownership — we trust the
    signed token over whatever the session_key cache happens to hold. This
    closes the hijack vector in issue #110 where a REST caller claimed
    Watcher's session_key and received Watcher's check-ins for 3 days.
    """

    @pytest.fixture
    def captured_events(self):
        return []

    @pytest.fixture
    def broadcaster_stub(self, captured_events):
        b = MagicMock()

        async def _record(**kwargs):
            captured_events.append(kwargs)

        b.broadcast_event = AsyncMock(side_effect=_record)
        return b

    @pytest.mark.asyncio
    async def test_token_mismatch_falls_through_regardless_of_fingerprint_mode(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        # Explicitly set fingerprint mode to "off" so we prove the
        # token check is independent of the fingerprint gate.
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        token_uuid = "99999999-8888-7777-6666-555555555555"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "hijacker_mcp_20260423",
            "bind_ip_ua": "ip-anything:ua-anything",
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        # PATH 2.8 sees the token, finds the real agent, and rebinds.
        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_id_from_metadata",
            new=AsyncMock(return_value="Watcher_mcp_20260420"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Watcher"),
        ), patch(
            "src.mcp_handlers.identity.resolution._cache_session",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            side_effect=RuntimeError("DB session rebind is non-fatal"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-aaaaaaaaaaab",
                resume=True,
                token_agent_uuid=token_uuid,
            )

        # PATH 2.8 should have taken over — result must name the token's
        # UUID, not the hijacker's cached UUID.
        assert result.get("agent_uuid") == token_uuid, (
            f"Token ownership must override cache hijack. Got: {result}"
        )
        assert result.get("source") == "token_rebind", (
            f"Expected PATH 2.8 token_rebind, got source={result.get('source')}"
        )

        # And the hijack event fires with the new path tag.
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events, (
            f"Token mismatch must emit identity_hijack_suspected. "
            f"Captured: {captured_events}"
        )
        payload = hijack_events[0].get("payload") or {}
        assert payload.get("path") == "path1_token_mismatch", (
            f"Expected path='path1_token_mismatch', got: {payload}"
        )
        assert payload.get("cached_uuid_prefix") == cached_uuid[:8]
        assert payload.get("token_uuid_prefix") == token_uuid[:8]

    @pytest.mark.asyncio
    async def test_matching_token_uuid_does_not_fall_through(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """When the token's aid matches the cached UUID, PATH 1 returns cached."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "cccccccc-dddd-eeee-ffff-000000000000"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Watcher_mcp_20260420",
            "bind_ip_ua": "ip-any:ua-any",
        }

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Watcher"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-cccccccccddd",
                resume=True,
                token_agent_uuid=agent_uuid,  # matches cache
            )

        # Happy path: token says what the cache says, PATH 1 returns cached.
        assert result.get("agent_uuid") == agent_uuid
        assert result.get("source") == "redis"
        assert captured_events == [], (
            f"Matching token should not emit hijack event. Got: {captured_events}"
        )
