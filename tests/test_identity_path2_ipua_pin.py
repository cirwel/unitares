"""PATH 2 IP:UA pin cross-check — Phase A (observation).

Council follow-up to #83 (PATH 1 fingerprint cross-check). `derive_session_key`
step 7 resolves an unauthenticated `onboard()` call (no continuity_token,
no explicit client_session_id, no mcp/oauth/x- session headers) to a
previously-pinned session by IP:UA fingerprint alone. Multiple same-family
agents on one machine silently adopt the first agent's UUID — the PATH 2
analogue of the bleeds closed for PATH 0 (#78/#81) and PATH 1 (#83).

This module locks in the observation-phase behavior of the helper
`_path2_ipua_pin_check` wired into `handle_onboard_v2`:

  - Fires `identity_hijack_suspected` with `path="path2_ipua_pin"` when the
    session-derivation source is `pinned_onboard_session` and the caller
    supplied no ownership proof.
  - In `log` mode the resume still proceeds after emission.
  - In `strict` mode the helper additionally returns `resume=False` so the
    caller mints a fresh identity.
  - In `off` mode, no check runs — no event, no resume flip.
  - Suppressed when the caller passes continuity_token, agent_uuid, or
    force_new. agent_id/client_session_id are deliberately not proof on this
    path because middleware/transport plumbing can populate them.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_pin_mode(monkeypatch):
    monkeypatch.delenv("UNITARES_IPUA_PIN_CHECK", raising=False)
    yield


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


class TestPath2IpuaPinCheck:
    @pytest.mark.asyncio
    async def test_default_mode_is_strict(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """With no env override, bare onboard() hitting the pin fallback must
        flip resume=False so a fresh identity is minted.

        Retires the PATH 2 IP:UA fingerprint pin as an implicit resume path.
        Identity ontology v2 (§85, `docs/ontology/identity.md`) names
        fingerprint-based cross-process-instance identity as performative —
        fresh process-instance = fresh identity unless a cryptographic resume
        signal is presented. Invariant #2 ("force_new is explicit opt-in
        only") is preserved: the flip to resume=False produces a fresh mint
        through the normal onboard flow, not via auto-force_new-as-fallback.
        """
        # Deliberate: no monkeypatch.setenv — exercise the compile-time default.

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        # Strict-by-default: resume forced False so the caller mints fresh.
        assert new_resume is False, (
            "Default mode must be 'strict' — bare onboard with no ownership "
            "proof must not adopt a fingerprint-pinned UUID. Retires "
            "§85 performative cross-process-instance identity. Got resume=True."
        )

        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events, "Default mode must still emit the event for visibility"
        assert (hijack_events[0].get("payload") or {}).get("mode") == "strict"

    @pytest.mark.asyncio
    async def test_log_mode_emits_and_leaves_resume_true(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "log")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        # Log mode: resume unchanged.
        assert new_resume is True

        # Event fired.
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events, (
            f"Log mode must emit identity_hijack_suspected. Got: {captured_events}"
        )
        evt = hijack_events[0]
        payload = evt.get("payload") or {}
        assert payload.get("path") == "path2_ipua_pin"
        assert payload.get("mode") == "log"
        assert payload.get("source") == "onboard_pin_fallback"
        assert payload.get("session_key_prefix") == "fp-ipua-abcdef:c"

    @pytest.mark.asyncio
    async def test_strict_mode_flips_resume_and_emits(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "strict")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        # Strict mode: resume forced False — caller mints fresh.
        assert new_resume is False

        # Event still fires with strict marker.
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events, (
            "Strict mode must still emit the event for visibility"
        )
        assert (hijack_events[0].get("payload") or {}).get("mode") == "strict"

    @pytest.mark.asyncio
    async def test_off_mode_does_not_emit_or_flip(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "off")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        assert new_resume is True
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events == [], (
            f"Off mode must not emit events. Got: {captured_events}"
        )

    @pytest.mark.asyncio
    async def test_non_pin_source_skips_check(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """When session derivation did not take the pin fallback, the helper
        is a no-op. Ensures we don't emit events for every onboard call."""
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "strict")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="continuity_token",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        assert new_resume is True
        assert captured_events == []

    @pytest.mark.parametrize(
        "proof_arg",
        [
            "continuity_token",
            "agent_uuid",
        ],
    )
    @pytest.mark.asyncio
    async def test_ownership_proof_suppresses_check(
        self, monkeypatch, captured_events, broadcaster_stub, proof_arg
    ):
        """Any caller-supplied ownership signal suppresses the PATH 2 gate
        even in strict mode — the caller has already asserted identity."""
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "strict")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={proof_arg: "some-value"},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        assert new_resume is True
        assert captured_events == []

    @pytest.mark.parametrize(
        "injected_arg",
        [
            "agent_id",
            "client_session_id",
        ],
    )
    @pytest.mark.asyncio
    async def test_middleware_injectable_args_do_not_suppress_check(
        self, monkeypatch, captured_events, broadcaster_stub, injected_arg
    ):
        """Middleware/transport-populated values must not neutralize strict mode.

        PR #98 regression: inject_identity populated arguments["agent_id"]
        before onboard's PATH 2 pin check, so the helper treated an arg-less
        onboard() as proof-bearing and resumed the pinned UUID.
        """
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "strict")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={injected_arg: "some-value"},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=False,
                resume=True,
            )

        assert new_resume is False
        hijack_events = [
            e for e in captured_events
            if e.get("event_type") == "identity_hijack_suspected"
        ]
        assert hijack_events

    @pytest.mark.asyncio
    async def test_force_new_suppresses_check(
        self, monkeypatch, captured_events, broadcaster_stub
    ):
        """force_new=True means the caller explicitly asked for a fresh
        identity — the pin-resume would never apply, so no event."""
        monkeypatch.setenv("UNITARES_IPUA_PIN_CHECK", "strict")

        from src.mcp_handlers.identity import handlers as h_mod

        with patch(
            "src.mcp_handlers.context.get_session_resolution_source",
            return_value="pinned_onboard_session",
        ), patch.object(h_mod, "_broadcaster", return_value=broadcaster_stub):
            new_resume = await h_mod._path2_ipua_pin_check(
                arguments={},
                base_session_key="fp-ipua-abcdef:claude",
                force_new=True,
                resume=True,
            )

        # resume unchanged (caller is already going to force_new).
        assert new_resume is True
        assert captured_events == []
