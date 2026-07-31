"""Tests for `src/mcp_handlers/dialectic/auth.py` — the dialectic identity resolver.

#1414: `resolve_dialectic_agent_id` must ALWAYS return an agent UUID. Every
consumer compares the result against `session.paused_agent_id` /
`session.reviewer_agent_id` (UUIDs) and writes it into
`core.dialectic_messages.agent_id` (UUIDs). Returning the public handle — which
is what `require_registered_agent` leaves in `arguments["agent_id"]` — makes
every submit path fail with "not registered".
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AUTH = "src.mcp_handlers.dialectic.auth"

PAUSED_UUID = "3b531b97-a39d-4b95-aeb3-91a1003c9685"
PAUSED_HANDLE = "Claude_Opus_5_20260730"
OTHER_UUID = "1f0c9e2a-7b44-4d6e-9c31-5aa8e0d21bb7"


def _meta(public_agent_id=None, structured_id=None, label=None):
    meta = MagicMock()
    meta.public_agent_id = public_agent_id
    meta.structured_id = structured_id
    meta.label = label
    meta.status = "active"
    return meta


def _server(metadata):
    server = MagicMock()
    server.agent_metadata = metadata
    server.ensure_metadata_loaded = MagicMock()
    return server


def _payload(error_seq):
    return json.loads(error_seq[0].text)


class TestCanonicalization:
    @pytest.mark.asyncio
    async def test_public_handle_resolves_to_uuid(self):
        """#1414 regression lock: the public handle must map to its UUID.

        This is the exact shape the one-call-review path produced —
        `require_registered_agent` rewrites arguments["agent_id"] to the
        public handle, and the nested submit_thesis then re-resolves it.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}
            )

        assert error is None
        assert agent_id == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_structured_id_resolves_to_uuid(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(structured_id="claude_opus_5:20260730")}
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "claude_opus_5:20260730"}
            )

        assert error is None
        assert agent_id == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_label_does_not_resolve(self):
        """No-lookup-by-label invariant: labels are self-claimed, never verified.

        `require_registered_agent` will match on label; the dialectic resolver
        deliberately will not.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {
            PAUSED_UUID: _meta(
                public_agent_id=PAUSED_HANDLE,
                label="Claude trajectory-identity audit",
            )
        }
        with patch(f"{AUTH}.mcp_server", _server(metadata)):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Claude trajectory-identity audit"}
            )

        assert agent_id is None
        assert error is not None
        assert _payload(error)["success"] is False

    @pytest.mark.asyncio
    async def test_uuid_passes_through_without_touching_postgres(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        pg = AsyncMock(return_value=True)
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_cache_uuid_falls_back_to_postgres(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        pg = AsyncMock(return_value=True)
        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        pg.assert_awaited_once_with(PAUSED_UUID)

    @pytest.mark.asyncio
    async def test_unknown_ref_errors_and_does_not_advise_onboard(self):
        """The old recovery told an already-registered caller to onboard(),
        which mints a NEW identity and orphans the session further."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        pg = AsyncMock(return_value=False)
        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 "src.mcp_handlers.identity.handlers._agent_exists_in_postgres", pg
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Nobody_In_Particular"}
            )

        assert agent_id is None
        data = _payload(error)
        assert data["success"] is False
        recovery = data.get("recovery") or {}
        # onboard() must not be offered as a route out: it mints a NEW identity,
        # which cannot help a caller who is already registered.
        assert "onboard" not in recovery.get("related_tools", [])
        assert "do not call onboard" in recovery.get("action", "").lower()

    @pytest.mark.asyncio
    async def test_no_agent_id_uses_require_registered_agent(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        with patch(f"{AUTH}.mcp_server", _server({})), \
             patch(
                 f"{AUTH}.require_registered_agent",
                 return_value=(PAUSED_UUID, None),
             ) as rra:
            agent_id, error = await resolve_dialectic_agent_id({})

        assert error is None
        assert agent_id == PAUSED_UUID
        rra.assert_called_once()


class TestForgeryResistance:
    @pytest.mark.asyncio
    async def test_forged_agent_uuid_is_ignored(self):
        """`_agent_uuid` is caller-reachable (params_step preserves unknown keys
        and it is not scrubbed), so the resolver must never read it. Preferring
        it would let `agent_id=<mine> _agent_uuid=<victim>` impersonate."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {
            OTHER_UUID: _meta(public_agent_id="Attacker_Handle"),
            PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE),
        }
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=OTHER_UUID,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "Attacker_Handle", "_agent_uuid": PAUSED_UUID}
            )

        assert error is None
        assert agent_id == OTHER_UUID, "resolver must not honour a wire _agent_uuid"

    @pytest.mark.asyncio
    async def test_forged_agent_uuid_cannot_pass_the_synthesis_participant_gate(self):
        """The behavior the #1414 commit actually fixed, locked at its own site.

        `handle_submit_synthesis` used to widen its participant allow-list with
        `resolve_agent_uuid(arguments, agent_id)`, which is literally
        `arguments.get("_agent_uuid") or agent_id` (support/coerce.py). That key
        is caller-reachable — params_step preserves unknown keys and nothing
        scrubs it — and submit_synthesis deliberately runs with
        enforce_session_ownership=False, so a registered NON-participant could
        name the paused agent in `_agent_uuid` and drive synthesis (and thus
        convergence + finalize_resolution) as them.

        The sibling test above pins the resolver, which never read `_agent_uuid`
        in either revision; only this one fails if the gate regresses.
        """
        import asyncio

        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        DIALECTIC = "src.mcp_handlers.dialectic.handlers"
        attacker, victim = OTHER_UUID, PAUSED_UUID

        session = MagicMock()
        session.session_id = "sess-forgery"
        session.paused_agent_id = victim
        session.reviewer_agent_id = None

        metadata = {attacker: _meta(public_agent_id="Attacker_Handle"),
                    victim: _meta(public_agent_id=PAUSED_HANDLE)}

        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=attacker,
             ), \
             patch(
                 f"{DIALECTIC}.load_session",
                 new_callable=AsyncMock, return_value=session,
             ), \
             patch(
                 f"{DIALECTIC}.get_session_lock",
                 new_callable=AsyncMock, return_value=asyncio.Lock(),
             ):
            result = await handle_submit_synthesis({
                "session_id": "sess-forgery",
                "agent_id": attacker,
                "_agent_uuid": victim,          # the forged claim
                "agrees": True,
                "proposed_conditions": ["c"],
                "root_cause": "rc",
                "reasoning": "r",
            })

        payload = _payload(result)
        assert payload.get("success") is False
        assert "not a participant" in payload["error"], (
            "a forged _agent_uuid must not buy participant eligibility"
        )
        # The rejection must name the CALLER, never the impersonated victim.
        assert attacker in payload["error"]
        assert victim not in payload["error"]


class TestHandleCollisions:
    """`public_agent_id` is NOT unique — 4294/4492 live identities with a handle
    share it (2026-07-31), 1848/2030 restricted to status='active'. A
    first-match-wins scan therefore resolves an honest caller onto a stranger's
    UUID. These lock the two halves of the fix: never guess, and let the bound
    agent's own alias resolve to the bound agent.
    """

    TWIN_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    TWIN_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    SHARED_HANDLE = "Claude_Code_20260617"  # 78 live holders

    def _twins(self, first, second):
        return {
            first: _meta(public_agent_id=self.SHARED_HANDLE),
            second: _meta(public_agent_id=self.SHARED_HANDLE),
        }

    @pytest.mark.asyncio
    async def test_ambiguous_handle_errors_instead_of_picking_one(self):
        """The synthesis path runs with enforce_session_ownership=False, so a
        mis-resolved UUID is what the participant allow-list is checked against
        and what lands in core.dialectic_messages.agent_id. It must fail closed.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = self._twins(self.TWIN_A, self.TWIN_B)
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=None,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": self.SHARED_HANDLE}
            )

        assert agent_id is None, "must not resolve an ambiguous handle at all"
        payload = _payload(error)
        assert payload["error_code"] == "AMBIGUOUS_AGENT_REF"
        assert payload["recovery"]["match_count"] == 2
        # Must not send them to onboard() — that mints a new identity.
        assert "onboard" not in payload["recovery"]["related_tools"]

    @pytest.mark.asyncio
    async def test_resolution_does_not_depend_on_cache_insertion_order(self):
        """The cache is built ORDER BY created_at DESC, so a first-match-wins
        scan silently returns the NEWEST holder of a shared handle. Resolution
        must not be a function of dict order.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        results = []
        for first, second in ((self.TWIN_A, self.TWIN_B), (self.TWIN_B, self.TWIN_A)):
            with patch(f"{AUTH}.mcp_server", _server(self._twins(first, second))), \
                 patch(
                     "src.mcp_handlers.context.get_context_agent_id",
                     return_value=None,
                 ):
                results.append(
                    await resolve_dialectic_agent_id({"agent_id": self.SHARED_HANDLE})
                )

        assert [r[0] for r in results] == [None, None]
        assert {_payload(r[1])["error_code"] for r in results} == {"AMBIGUOUS_AGENT_REF"}

    @pytest.mark.asyncio
    async def test_bound_agent_own_handle_resolves_to_bound_uuid(self):
        """The live-reachable case. `params_step.inject_identity` only lets a
        non-bound agent_id through to a dialectic call when it IS an alias of
        the bound agent, and `_bound_identity_aliases` includes public_agent_id.
        So the ordinary caller passing their own handle must resolve to
        THEMSELVES even though a twin shares that handle — otherwise
        verify_agent_ownership rejects the rightful owner (91% of active agents
        carrying a handle share it).
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        # Twin listed FIRST: a naive scan would return TWIN_A.
        metadata = self._twins(self.TWIN_A, self.TWIN_B)
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=self.TWIN_B,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": self.SHARED_HANDLE}
            )

        assert error is None
        assert agent_id == self.TWIN_B, "bound identity must win over a twin"

    @pytest.mark.asyncio
    async def test_bound_alias_survives_the_ownership_gate(self):
        """End-to-end of the false-rejection: thesis/antithesis pass
        enforce_session_ownership=True, and verify_agent_ownership only accepts
        the bound UUID. Pre-fix this returned AUTH_REQUIRED for the owner.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = self._twins(self.TWIN_A, self.TWIN_B)

        def _real_ownership(agent_id, arguments):
            return agent_id == self.TWIN_B  # bound == TWIN_B

        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=self.TWIN_B,
             ), \
             patch(
                 "src.mcp_handlers.utils.verify_agent_ownership",
                 side_effect=_real_ownership,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": self.SHARED_HANDLE}, enforce_session_ownership=True
            )

        assert error is None, "rightful owner must not be locked out"
        assert agent_id == self.TWIN_B

    @pytest.mark.asyncio
    async def test_bound_label_resolves_to_bound_uuid_without_fleet_label_lookup(self):
        """Label is accepted ONLY as a bound-identity alias (it resolves to the
        caller's own server-verified UUID). It must still never be used to find
        a DIFFERENT agent — that is the no-lookup-by-label invariant.
        """
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {
            OTHER_UUID: _meta(label="shared-label"),
            PAUSED_UUID: _meta(label="shared-label"),
        }
        # Bound agent passing its own label -> its own UUID.
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=PAUSED_UUID,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "shared-label"}
            )
        assert error is None
        assert agent_id == PAUSED_UUID

        # Unbound caller naming someone else's label -> still unresolvable.
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=None,
             ), \
             patch(
                 f"{AUTH}._looks_like_uuid", return_value=False,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": "shared-label"}
            )
        assert agent_id is None
        assert _payload(error)["recovery"]["error_type"] == "agent_ref_unresolvable"


class TestSessionOwnership:
    @pytest.mark.asyncio
    async def test_ownership_check_receives_the_uuid_not_the_handle(self):
        """The ownership gate compares against `get_context_agent_id()`, which is
        a UUID. Handing it a public handle made it fail closed for the rightful
        owner — the second half of the #1414 break."""
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=PAUSED_UUID,
             ), \
             patch(
                 "src.mcp_handlers.utils.verify_agent_ownership",
                 return_value=True,
             ) as vao:
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}, enforce_session_ownership=True
            )

        assert error is None
        assert agent_id == PAUSED_UUID
        assert vao.call_args[0][0] == PAUSED_UUID

    @pytest.mark.asyncio
    async def test_ownership_check_rejects_another_agent(self):
        from src.mcp_handlers.dialectic.auth import resolve_dialectic_agent_id

        metadata = {PAUSED_UUID: _meta(public_agent_id=PAUSED_HANDLE)}
        with patch(f"{AUTH}.mcp_server", _server(metadata)), \
             patch(
                 "src.mcp_handlers.context.get_context_agent_id",
                 return_value=OTHER_UUID,
             ), \
             patch(
                 "src.mcp_handlers.utils.verify_agent_ownership",
                 return_value=False,
             ):
            agent_id, error = await resolve_dialectic_agent_id(
                {"agent_id": PAUSED_HANDLE}, enforce_session_ownership=True
            )

        assert agent_id is None
        assert _payload(error)["error_code"] == "AUTH_REQUIRED"
