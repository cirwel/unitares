"""PATH 2.75 (X-Agent-Id UUID recovery) substrate-over-HTTP gate (#802 parity).

PATH 2.75 lets a freshly-created dispatch session rebind to an existing UUID
supplied via the ``X-Agent-Id`` header (reconnection when the session key
changed, e.g. a Pi restart). It was the one sibling resume path that did NOT
apply ``_substrate_http_reject`` — the gate PATH 1 (cache/prefix) and PATH 2
(PG session) already enforce — so a copyable ``X-Agent-Id`` header bearing a
substrate resident's UUID could rebind to it over HTTP, evading the #802 closure.

This pins both directions: the legitimate non-substrate / UDS recovery still
works, and the substrate-over-HTTP exfiltration path is refused (the session
keeps its freshly-created identity).

The header itself proves nothing: anyone can send it. A rebind also needs
proof that the caller owns the named agent, a verified continuity token for
that agent, or a UDS peer whose substrate attestation for that agent is
accepted; without one the session keeps its freshly-created identity and no
binding is cached.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.middleware.identity_step import _maybe_recover_via_x_agent_id

SUBSTRATE_UUID = "02e168be-4264-527b-9643-9c737dd309e4"
PLAIN_UUID = "00000000-1111-2222-3333-444444444444"

_HANDLERS = "src.mcp_handlers.identity.handlers"
_RES = "src.mcp_handlers.identity.resolution"
_GATE = "src.substrate.handler_gate"


def _fresh_result(created_uuid="aaaaaaaa-0000-0000-0000-000000000000"):
    return {"created": True, "agent_uuid": created_uuid}


def _signals(peer_pid=None, fp="ip-x:ua-x"):
    return SimpleNamespace(
        peer_pid=peer_pid,
        ip_ua_fingerprint=fp,
        transport="uds" if peer_pid is not None else "mcp",
    )


def _claim(label="com.unitares.sentinel"):
    return SimpleNamespace(expected_launchd_label=label)


def _verdict(accepted, failure_code=None):
    from src.substrate.verification import VerificationResult
    return VerificationResult(
        accepted=accepted,
        reason="accepted" if accepted else "rejected",
        failure_code=failure_code,
    )


# ── isolated branch coverage (gate mocked) ─────────────────────────────────

class TestRecoveryBranches:
    @pytest.mark.asyncio
    async def test_header_alone_does_not_rebind(self):
        """A known non-substrate UUID in the header, over HTTP, with no proof:
        the session keeps its freshly-created identity and nothing is cached."""
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "sess-key", signals=_signals(peer_pid=None),
            )
        assert bound == original
        assert result["agent_uuid"] == original
        assert result["created"] is True
        assert result.get("source") != "x_agent_id_recovery"
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_token_for_another_agent_does_not_rebind(self):
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "sess-key",
                signals=_signals(peer_pid=None), token_agent_uuid=SUBSTRATE_UUID,
            )
        assert bound == original
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebind_with_matching_token(self):
        """A verified continuity token for the header's agent is proof."""
        result = _fresh_result()
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "sess-key",
                signals=_signals(peer_pid=None), token_agent_uuid=PLAIN_UUID,
            )
        assert bound == PLAIN_UUID
        assert result["agent_uuid"] == PLAIN_UUID
        assert result["created"] is False
        assert result["persisted"] is True
        assert result["source"] == "x_agent_id_recovery"
        cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rebind_over_uds_peer_with_accepted_attestation(self):
        """A UDS peer whose substrate attestation for the named agent is
        accepted is proof (the resident reconnection)."""
        result = _fresh_result()
        attest = AsyncMock(return_value=_verdict(True))
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_GATE}.verify_substrate_at_resume", new=attest), \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "sess-key", signals=_signals(peer_pid=4321),
            )
        assert bound == SUBSTRATE_UUID
        assert result["source"] == "x_agent_id_recovery"
        cache.assert_awaited_once()
        attest.assert_awaited_once_with(SUBSTRATE_UUID, 4321)

    @pytest.mark.asyncio
    async def test_uds_peer_naming_unclaimed_agent_does_not_rebind(self):
        """A UDS peer alone is not proof: an agent with no substrate claim
        (attestation returns None) needs a matching token."""
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_GATE}.verify_substrate_at_resume", new=AsyncMock(return_value=None)), \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "sess-key", signals=_signals(peer_pid=4321),
            )
        assert bound == original
        assert result["created"] is True
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uds_peer_with_rejected_attestation_does_not_rebind(self):
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_GATE}.verify_substrate_at_resume",
                   new=AsyncMock(return_value=_verdict(False, "label_mismatch"))), \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "sess-key", signals=_signals(peer_pid=4321),
            )
        assert bound == original
        assert result["created"] is True
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uds_peer_with_matching_token_skips_attestation(self):
        """A matching token is proof by itself, over UDS as over HTTP."""
        result = _fresh_result()
        attest = AsyncMock(return_value=None)
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_GATE}.verify_substrate_at_resume", new=attest), \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "sess-key",
                signals=_signals(peer_pid=4321), token_agent_uuid=PLAIN_UUID,
            )
        assert bound == PLAIN_UUID
        cache.assert_awaited_once()
        attest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_signals_does_not_rebind(self):
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(result, PLAIN_UUID, "sess-key")
        assert bound == original
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_substrate_uuid_refused_no_rebind(self):
        """A substrate UUID whose gate refuses (over HTTP) must NOT rebind — the
        session keeps its freshly-created identity (safe denial)."""
        result = _fresh_result()
        original = result["agent_uuid"]
        refusal = {"resume_failed": True, "error": "substrate_anchored_uuid_requires_uds"}
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=refusal)):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "sess-key",
                signals=_signals(peer_pid=None), token_agent_uuid=SUBSTRATE_UUID,
            )
        assert bound == original
        assert result["agent_uuid"] == original
        assert result["created"] is True
        assert "source" not in result or result["source"] != "x_agent_id_recovery"
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_created_is_noop(self):
        result = {"created": False, "agent_uuid": PLAIN_UUID}
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)) as exists:
            bound = await _maybe_recover_via_x_agent_id(result, SUBSTRATE_UUID, "k")
        assert bound == PLAIN_UUID
        exists.assert_not_awaited()  # short-circuits before any DB work

    @pytest.mark.asyncio
    async def test_non_uuid_header_is_noop(self):
        result = _fresh_result("bbbbbbbb-0000-0000-0000-000000000000")
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)) as exists:
            bound = await _maybe_recover_via_x_agent_id(result, "not-a-uuid", "k")
        assert bound == "bbbbbbbb-0000-0000-0000-000000000000"
        exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_uuid_is_noop(self):
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=False)), \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)) as gate:
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "k", signals=_signals(peer_pid=4321),
            )
        assert bound == original
        gate.assert_not_awaited()  # never reaches the gate for an unknown UUID


# ── end-to-end wiring through the REAL gate (its deps mocked) ───────────────

class TestGateWiring:
    @pytest.mark.asyncio
    async def test_substrate_over_http_refused(self):
        """No peer_pid (HTTP) + substrate claim → real gate refuses → no rebind."""
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=_signals(peer_pid=None)), \
             patch("src.substrate.verification.fetch_substrate_claim", new=AsyncMock(return_value=_claim())):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "k",
                signals=_signals(peer_pid=None), token_agent_uuid=SUBSTRATE_UUID,
            )
        assert bound == original
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_substrate_over_uds_rebinds(self):
        """peer_pid present (UDS) and the real handler gate accepts the peer's
        attestation → legitimate resident reconnection still recovers the
        substrate UUID."""
        result = _fresh_result()
        verify = MagicMock(return_value=_verdict(True))
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=_signals(peer_pid=4321)), \
             patch(f"{_GATE}.fetch_substrate_claim", new=AsyncMock(return_value=_claim())), \
             patch(f"{_GATE}.verify_substrate_claim", new=verify):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "k", signals=_signals(peer_pid=4321),
            )
        assert bound == SUBSTRATE_UUID
        assert result["agent_uuid"] == SUBSTRATE_UUID
        assert result["source"] == "x_agent_id_recovery"
        verify.assert_called_once()

    @pytest.mark.asyncio
    async def test_substrate_over_uds_rejected_attestation_refused(self):
        """A same-user UDS peer that is not the registered substrate: the real
        handler gate rejects the attestation → no rebind, nothing cached."""
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch("src.mcp_handlers.context.get_session_signals", return_value=_signals(peer_pid=4321)), \
             patch(f"{_GATE}.fetch_substrate_claim", new=AsyncMock(return_value=_claim())), \
             patch(f"{_GATE}.verify_substrate_claim",
                   new=MagicMock(return_value=_verdict(False, "label_mismatch"))):
            bound = await _maybe_recover_via_x_agent_id(
                result, SUBSTRATE_UUID, "k", signals=_signals(peer_pid=4321),
            )
        assert bound == original
        assert result["created"] is True
        cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unclaimed_uuid_over_uds_without_token_refused(self):
        """UDS peer naming a non-substrate agent with no token: the real gate
        finds no claim → no proof → no rebind."""
        result = _fresh_result()
        original = result["agent_uuid"]
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch("src.mcp_handlers.context.get_session_signals", return_value=_signals(peer_pid=4321)), \
             patch(f"{_GATE}.fetch_substrate_claim", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(
                result, PLAIN_UUID, "k", signals=_signals(peer_pid=4321),
            )
        assert bound == original
        cache.assert_not_awaited()


# ── through the dispatch middleware, strict identity off ────────────────────

class TestDispatchWiring:
    @pytest.mark.legacy_identity_defaults
    @pytest.mark.asyncio
    async def test_foreign_header_stays_on_the_minted_identity(self):
        """With STRICT_IDENTITY_REQUIRED off, a call that mints carries another
        agent's UUID in X-Agent-Id and no proof: it stays bound to the minted
        identity, and no session binding to the named agent is cached."""
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.identity_step import resolve_identity
        from tests.test_identity_premint_recovery import (
            FakeRedis,
            FakeSignals,
            _make_db,
            _patches,
            _Stack,
        )

        signals = FakeSignals(x_agent_id=PLAIN_UUID)
        with _Stack(_patches(FakeRedis(), _make_db())), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
             patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache:
            _, _, ctx = await resolve_identity("sync_state", {}, DispatchContext())

        assert ctx.bound_agent_id is not None
        assert ctx.bound_agent_id != PLAIN_UUID
        assert ctx.identity_result["created"] is True
        assert ctx.identity_result.get("source") != "x_agent_id_recovery"
        assert all(PLAIN_UUID not in call.args for call in cache.await_args_list)
