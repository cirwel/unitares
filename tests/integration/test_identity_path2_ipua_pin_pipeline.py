"""Wave 3 §7.3 / §14 prereq PR #4 — PATH 2 IPUA pin pipeline integration test.

Drives ``handle_onboard_v2`` END-TO-END through the real pipeline:

    transport signals → derive_session_key (PATH 7 pin lookup against a
    Redis stub) → session_resolution_source contextvar → _path2_ipua_pin_check
    → resolve_session_identity → response payload

Only the I/O boundaries (Redis, PG, broadcaster, mcp_server singleton) are
stubbed; the resolution-source contextvar wiring between ``session.py`` and
``handlers.py`` — the part the unit tests in
``tests/test_identity_path2_ipua_pin.py`` bypass by patching
``get_session_resolution_source`` directly — runs for real.

The load-bearing assertion is the **strict-mode passthrough invariant**
(RFC §3.1 surface F: "IPUA pin treats agent_id as proof"; the invariant
CANNOT be relaxed): an onboard that resolves through the IP:UA pin while
carrying ``agent_id`` in arguments must pass through and resume the pinned
identity even in strict mode — strict only forces a fresh mint when the
caller presented NO ownership proof.

Per RFC §7.3, the Wave 3 BEAM identity-middleware port must reuse this test
against the BEAM-side dispatch entry: ``drive_onboard`` is the single entry
point to swap.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import parse_result


# --- Pipeline fixture data --------------------------------------------------

PINNED_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PINNED_SESSION_KEY = "pinned-session-ipua-1"

# IP:UA fingerprint as captured at the transport layer. PATH 7 extracts the
# UA hash (parts[1]) and probes ``recent_onboard:ua:<hash>``.
IPUA_FINGERPRINT = "203.0.113.7:abc123"
PIN_REDIS_KEY = "recent_onboard:ua:abc123"


def _make_raw_redis():
    """Raw Redis stub: holds exactly one onboard pin."""
    raw = AsyncMock()

    async def _get(key):
        if key == PIN_REDIS_KEY:
            return json.dumps(
                {"client_session_id": PINNED_SESSION_KEY, "agent_uuid": PINNED_UUID}
            )
        return None

    raw.get = AsyncMock(side_effect=_get)
    raw.ttl = AsyncMock(return_value=600)
    raw.expire = AsyncMock(return_value=True)
    raw.set = AsyncMock(return_value=True)
    raw.setex = AsyncMock(return_value=True)
    raw.delete = AsyncMock(return_value=0)
    return raw


def _make_session_cache(pinned_identity_present: bool):
    """Session-resolution cache (PATH 1): optionally knows the pinned binding."""
    cache = MagicMock()

    async def _get(key):
        if pinned_identity_present and key == PINNED_SESSION_KEY:
            return {
                "agent_id": PINNED_UUID,
                "display_agent_id": "PinnedAgent_20260101",
            }
        return None

    cache.get = AsyncMock(side_effect=_get)
    cache.bind = AsyncMock()
    return cache


def _make_db():
    db = AsyncMock()
    db.init = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.get_identity = AsyncMock(return_value=None)
    db.get_agent = AsyncMock(return_value=None)
    db.get_agent_label = AsyncMock(return_value="PinnedAgent_20260101")
    db.get_agent_status = AsyncMock(return_value="active")
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.create_session = AsyncMock()
    db.update_session_activity = AsyncMock()
    db.find_agent_by_label = AsyncMock(return_value=None)
    db.get_agent_thread_info = AsyncMock(return_value=None)
    db.get_thread_nodes = AsyncMock(return_value=[])
    return db


def _discard_task(coro, **kwargs):
    """Swallow fire-and-forget tasks (persistence writes, broadcasts)."""
    try:
        coro.close()
    except Exception:
        pass
    t = MagicMock()
    t.cancel = MagicMock()
    return t


async def drive_onboard(
    arguments: dict,
    *,
    pin_mode: str,
    pinned_identity_present: bool = True,
    monkeypatch,
):
    """Run one onboard through the Python dispatch entry with the pin staged.

    Returns ``(response_payload, captured_events, raw_redis)``.

    This is the §7.3 swap point: the Wave 3 BEAM identity-middleware port
    re-implements THIS function against the BEAM-side dispatch entry and
    re-runs the same test bodies unchanged.
    """
    from src.mcp_handlers.context import SessionSignals
    from src.mcp_handlers.identity import handlers as h_mod
    from src.mcp_handlers.identity.handlers import handle_onboard_v2

    monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", pin_mode)
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "ipua-pipeline-secret")

    raw_redis = _make_raw_redis()
    session_cache = _make_session_cache(pinned_identity_present)
    db = _make_db()

    captured_events: list[dict] = []
    broadcaster = MagicMock()

    async def _record(**kwargs):
        captured_events.append(kwargs)

    broadcaster.broadcast_event = AsyncMock(side_effect=_record)

    signals = SessionSignals(ip_ua_fingerprint=IPUA_FINGERPRINT, transport="rest")

    async def _get_raw():
        return raw_redis

    with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
         patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
         patch("src.cache.get_session_cache", return_value=session_cache), \
         patch("src.mcp_handlers.identity.resolution._get_redis", return_value=session_cache), \
         patch("src.mcp_handlers.identity.resolution._agent_exists_in_postgres", AsyncMock(return_value=True)), \
         patch("src.mcp_handlers.identity.handlers.get_db", return_value=db), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
         patch("src.cache.redis_client.get_redis", new=_get_raw), \
         patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_client_hint", return_value=None), \
         patch("src.mcp_handlers.context.update_context_agent_id"), \
         patch("asyncio.create_task", side_effect=_discard_task), \
         patch("src.mcp_handlers.shared.get_mcp_server",
               return_value=SimpleNamespace(agent_metadata={})), \
         patch("src.mcp_handlers.identity.shared._register_uuid_prefix"), \
         patch.object(h_mod, "_broadcaster", return_value=broadcaster):
        result = await handle_onboard_v2(dict(arguments))

    return parse_result(result), captured_events, raw_redis


def _hijack_events(events: list[dict]) -> list[dict]:
    return [
        e for e in events if e.get("event_type") == "identity_hijack_suspected"
    ]


# --- §7.3 scenarios -----------------------------------------------------------


class TestStrictModePassthroughInvariant:
    """The invariant named by RFC §3.1 surface F and §7.3: agent_id IS proof."""

    @pytest.mark.asyncio
    async def test_agent_id_passthrough_resumes_pinned_identity(self, monkeypatch):
        """Strict mode + agent_id in arguments → the pin-resolved session
        resumes the pinned identity. No fresh mint, no hijack alert."""
        payload, events, _ = await drive_onboard(
            {"agent_id": "PinnedAgent_20260101"},
            pin_mode="strict",
            monkeypatch=monkeypatch,
        )

        assert payload.get("success") is True, payload
        assert payload.get("uuid") == PINNED_UUID, (
            "agent_id is ownership proof (project_ipua-pin-agent-id-proof) — "
            "strict mode must pass the pin-resolved session through to resume, "
            f"not mint fresh. Got uuid={payload.get('uuid')!r}"
        )
        assert payload.get("is_new") is not True, payload
        assert _hijack_events(events) == [], (
            "Proof-carrying onboard must not raise identity_hijack_suspected"
        )

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_resumes_pinned_identity(self, monkeypatch):
        """agent_uuid is equivalent proof — same passthrough contract."""
        payload, events, _ = await drive_onboard(
            {"agent_uuid": PINNED_UUID},
            pin_mode="strict",
            monkeypatch=monkeypatch,
        )

        assert payload.get("success") is True, payload
        assert payload.get("uuid") == PINNED_UUID, payload
        assert _hijack_events(events) == []


class TestStrictModeNoProofForcesFreshMint:
    @pytest.mark.asyncio
    async def test_name_only_onboard_mints_fresh_and_emits(self, monkeypatch):
        """``name`` clears the S13 arg-less gate but is NOT ownership proof —
        strict mode must refuse the pin-resume and mint a fresh identity,
        emitting the hijack alert with the PATH 2 marker."""
        payload, events, _ = await drive_onboard(
            {"name": "SameMachineNewcomer"},
            pin_mode="strict",
            monkeypatch=monkeypatch,
        )

        assert payload.get("success") is True, payload
        fresh_uuid = payload.get("uuid")
        assert fresh_uuid and fresh_uuid != PINNED_UUID, (
            "Strict mode with no ownership proof must NOT adopt the pinned "
            f"UUID. Got uuid={fresh_uuid!r}"
        )
        assert payload.get("is_new") is True, payload

        hijacks = _hijack_events(events)
        assert hijacks, "Strict refusal must stay visible on the broadcast channel"
        hijack_payload = hijacks[0].get("payload") or {}
        assert hijack_payload.get("path") == "path2_ipua_pin"
        assert hijack_payload.get("mode") == "strict"
        assert hijack_payload.get("source") == "onboard_pin_fallback"

    @pytest.mark.asyncio
    async def test_strict_refusal_leaves_pin_intact(self, monkeypatch):
        """The pin entry survives the strict refusal so the legitimate owner
        can still resume by presenting proof (handlers.py contract)."""
        _, _, raw_redis = await drive_onboard(
            {"name": "SameMachineNewcomer"},
            pin_mode="strict",
            monkeypatch=monkeypatch,
        )

        deleted_keys = [c.args[0] for c in raw_redis.delete.await_args_list]
        assert PIN_REDIS_KEY not in deleted_keys, (
            "Strict mode must leave the pin intact — only the resume flips"
        )


class TestLogModeStillResumes:
    @pytest.mark.asyncio
    async def test_name_only_onboard_resumes_with_alert(self, monkeypatch):
        """Observation mode: same no-proof call resumes the pinned identity
        but the alert still fires. Pins the strict↔log behavioral delta at
        the handler boundary for the BEAM-port parity run."""
        payload, events, _ = await drive_onboard(
            {"name": "SameMachineNewcomer"},
            pin_mode="log",
            monkeypatch=monkeypatch,
        )

        assert payload.get("success") is True, payload
        assert payload.get("uuid") == PINNED_UUID, (
            "Log mode observes but does not block the pin-resume"
        )

        hijacks = _hijack_events(events)
        assert hijacks, "Log mode must emit the observation event"
        assert (hijacks[0].get("payload") or {}).get("mode") == "log"
