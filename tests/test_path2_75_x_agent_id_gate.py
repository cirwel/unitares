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
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.middleware.identity_step import _maybe_recover_via_x_agent_id

SUBSTRATE_UUID = "f92dcea8-4786-412a-a0eb-362c273382f5"
PLAIN_UUID = "00000000-1111-2222-3333-444444444444"

_HANDLERS = "src.mcp_handlers.identity.handlers"
_RES = "src.mcp_handlers.identity.resolution"


def _fresh_result(created_uuid="aaaaaaaa-0000-0000-0000-000000000000"):
    return {"created": True, "agent_uuid": created_uuid}


def _signals(peer_pid=None, fp="ip-x:ua-x"):
    return SimpleNamespace(peer_pid=peer_pid, ip_ua_fingerprint=fp)


def _claim(label="com.unitares.sentinel"):
    return SimpleNamespace(expected_launchd_label=label)


# ── isolated branch coverage (gate mocked) ─────────────────────────────────

class TestRecoveryBranches:
    @pytest.mark.asyncio
    async def test_non_substrate_uuid_rebinds(self):
        """The legitimate recovery: a known non-substrate UUID over HTTP rebinds
        the freshly-created session (the branch that previously had no test)."""
        result = _fresh_result()
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()) as cache, \
             patch(f"{_RES}._substrate_http_reject", new=AsyncMock(return_value=None)):
            bound = await _maybe_recover_via_x_agent_id(result, PLAIN_UUID, "sess-key")
        assert bound == PLAIN_UUID
        assert result["agent_uuid"] == PLAIN_UUID
        assert result["created"] is False
        assert result["persisted"] is True
        assert result["source"] == "x_agent_id_recovery"
        cache.assert_awaited_once()

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
            bound = await _maybe_recover_via_x_agent_id(result, SUBSTRATE_UUID, "sess-key")
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
            bound = await _maybe_recover_via_x_agent_id(result, PLAIN_UUID, "k")
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
            bound = await _maybe_recover_via_x_agent_id(result, SUBSTRATE_UUID, "k")
        assert bound == original
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_substrate_over_uds_rebinds(self):
        """peer_pid present (UDS, kernel-attested) → real gate defers → legitimate
        Pi/Lumen reconnection still recovers the substrate UUID."""
        result = _fresh_result()
        with patch(f"{_HANDLERS}._agent_exists_in_postgres", new=AsyncMock(return_value=True)), \
             patch(f"{_HANDLERS}._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=_signals(peer_pid=4321)), \
             patch("src.substrate.verification.fetch_substrate_claim", new=AsyncMock(return_value=_claim())):
            bound = await _maybe_recover_via_x_agent_id(result, SUBSTRATE_UUID, "k")
        assert bound == SUBSTRATE_UUID
        assert result["agent_uuid"] == SUBSTRATE_UUID
        assert result["source"] == "x_agent_id_recovery"
