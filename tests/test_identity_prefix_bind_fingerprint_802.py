"""Per-path prefix-bind fingerprint hardening (#802).

The `agent-{uuid12}` prefix shape is resolvable from a victim's UUID alone, so
PATH 1 resume by that shape has no ownership proof. The global
`UNITARES_SESSION_FINGERPRINT_CHECK` cannot safely flip to `strict` fleet-wide
(IP:UA is legitimately shared by co-resident localhost clients), so #802 adds a
*per-path* flag, `UNITARES_PREFIX_BIND_FINGERPRINT`, scoped to the prefix shape.

This module locks in the per-path behavior:

  - Default `off` → byte-identical to prior behavior (no per-path check).
  - `log`/`strict` activate the prefix-scoped check, which — unlike the global
    check — treats an ABSENT binding/current fingerprint on a prefix key as
    non-authorizing (closes the bind_ip_ua-absent hole).
  - The per-path mode takes precedence when set stricter than the global mode.
  - Non-prefix keys are never affected by the per-path flag (scoping).

The global-flag behavior itself is covered by test_identity_path1_fingerprint.py;
here we only exercise what the per-path flag adds.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_modes(monkeypatch):
    monkeypatch.delenv("UNITARES_SESSION_FINGERPRINT_CHECK", raising=False)
    monkeypatch.delenv("UNITARES_PREFIX_BIND_FINGERPRINT", raising=False)
    yield


def _signals_with_fp(fp):
    sig = MagicMock()
    sig.ip_ua_fingerprint = fp
    return sig


@pytest.fixture
def captured_events():
    return []


@pytest.fixture
def broadcaster_stub(captured_events):
    b = MagicMock()

    async def _record(**kwargs):
        captured_events.append(kwargs)

    b.broadcast_event = AsyncMock(side_effect=_record)
    return b


def _hijack_events(captured_events):
    return [
        e for e in captured_events
        if e.get("event_type") == "identity_hijack_suspected"
    ]


class TestPerPathAbsentFingerprintHole:
    """An absent binding fingerprint on a prefix key is non-authorizing under
    the per-path flag — this is the bind_ip_ua-absent hole #802 closes."""

    @pytest.mark.asyncio
    async def test_strict_absent_bind_fp_falls_through(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        # Global OFF proves the per-path flag alone drives the denial.
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")
        monkeypatch.setenv("UNITARES_PREFIX_BIND_FINGERPRINT", "strict")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "11111111-2222-3333-4444-555555555555"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_no_bindfp",
            # NO bind_ip_ua — the hole
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-attacker:ua-attacker"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ), patch.object(
            res, "_agent_exists_in_postgres", new=AsyncMock(return_value=False)
        ), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            side_effect=RuntimeError("PATH 2 must not resume the victim"),
        ), patch(
            "src.mcp_handlers.identity.resolution._generate_agent_id",
            return_value="Claude_fresh",
        ):
            result = await res.resolve_session_identity(
                session_key="agent-111111111222",
                resume=True,
                persist=False,
            )

        assert result.get("agent_uuid") != cached_uuid, (
            f"Strict per-path mode must refuse to reuse a prefix-key binding "
            f"with no recorded fingerprint. Got: {result}"
        )
        evts = _hijack_events(captured_events)
        assert evts, f"Absent bind fp must emit. Captured: {captured_events}"
        payload = evts[0].get("payload") or {}
        assert payload.get("reason") == "no_bind_fingerprint"
        assert payload.get("prefix_scoped") is True
        assert payload.get("mode") == "strict"

    @pytest.mark.asyncio
    async def test_log_absent_bind_fp_emits_but_proceeds(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")
        monkeypatch.setenv("UNITARES_PREFIX_BIND_FINGERPRINT", "log")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "22222222-3333-4444-5555-666666666666"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_log_nobindfp",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_log_nobindfp"),
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
                session_key="agent-222222222333",
                resume=True,
            )

        # Log mode: still resumes...
        assert result.get("source") == "redis"
        assert result.get("agent_uuid") == cached_uuid
        # ...but the absent-fp violation is observed.
        evts = _hijack_events(captured_events)
        assert evts and (evts[0].get("payload") or {}).get("reason") == "no_bind_fingerprint"

    @pytest.mark.asyncio
    async def test_strict_absent_current_fp_falls_through(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """No current request fingerprint is also non-authorizing for a prefix key."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")
        monkeypatch.setenv("UNITARES_PREFIX_BIND_FINGERPRINT", "strict")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "33333333-4444-5555-6666-777777777777"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_no_curfp",
            "bind_ip_ua": "ip-orig:ua-orig",  # bind fp present, but...
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=None,  # ...no current fingerprint
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster_stub,
        ), patch.object(
            res, "_agent_exists_in_postgres", new=AsyncMock(return_value=False)
        ), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            side_effect=RuntimeError("PATH 2 must not resume the victim"),
        ), patch(
            "src.mcp_handlers.identity.resolution._generate_agent_id",
            return_value="Claude_fresh",
        ):
            result = await res.resolve_session_identity(
                session_key="agent-333333333444",
                resume=True,
                persist=False,
            )

        assert result.get("agent_uuid") != cached_uuid
        evts = _hijack_events(captured_events)
        assert evts and (evts[0].get("payload") or {}).get("reason") == "no_current_fingerprint"


class TestPerPathPrecedenceAndScoping:

    @pytest.mark.asyncio
    async def test_strict_overrides_global_off_on_mismatch(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """Global off would skip; per-path strict denies a present-fp mismatch."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")
        monkeypatch.setenv("UNITARES_PREFIX_BIND_FINGERPRINT", "strict")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "44444444-5555-6666-7777-888888888888"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_override",
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
        ), patch.object(
            res, "_agent_exists_in_postgres", new=AsyncMock(return_value=False)
        ), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            side_effect=RuntimeError("PATH 2 must not resume the victim"),
        ), patch(
            "src.mcp_handlers.identity.resolution._generate_agent_id",
            return_value="Claude_fresh",
        ):
            result = await res.resolve_session_identity(
                session_key="agent-444444444555",
                resume=True,
                persist=False,
            )

        assert result.get("agent_uuid") != cached_uuid
        evts = _hijack_events(captured_events)
        assert evts and (evts[0].get("payload") or {}).get("reason") == "fingerprint_mismatch"
        assert (evts[0].get("payload") or {}).get("mode") == "strict"

    @pytest.mark.asyncio
    async def test_perpath_does_not_affect_nonprefix_key(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """A non-`agent-` session_key is outside the per-path flag's scope:
        even strict + absent fp must resume untouched."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "off")
        monkeypatch.setenv("UNITARES_PREFIX_BIND_FINGERPRINT", "strict")

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "55555555-6666-7777-8888-999999999999"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_nonprefix",
            # no bind_ip_ua — but key is not the prefix shape, so out of scope
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_nonprefix"),
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
                session_key="mcp-session-abcdef0123",
                resume=True,
            )

        assert result.get("source") == "redis"
        assert result.get("agent_uuid") == cached_uuid
        assert _hijack_events(captured_events) == [], (
            "Per-path flag must not touch non-prefix keys"
        )

    @pytest.mark.asyncio
    async def test_default_off_absent_bind_fp_is_silent(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """Default (per-path unset) under global log: an absent bind fp on a
        prefix key stays silent — the inert default, identical to today and to
        the legacy backward-compat contract."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "log")
        # UNITARES_PREFIX_BIND_FINGERPRINT intentionally unset → "off"

        from src.mcp_handlers.identity import resolution as res

        cached_uuid = "66666666-7777-8888-9999-000000000000"
        cached_payload = {
            "agent_id": cached_uuid,
            "display_agent_id": "Claude_default_off",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_default_off"),
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
                session_key="agent-666666666777",
                resume=True,
            )

        assert result.get("source") == "redis"
        assert result.get("agent_uuid") == cached_uuid
        assert _hijack_events(captured_events) == [], (
            "Per-path default off must be silent on absent bind fp"
        )
