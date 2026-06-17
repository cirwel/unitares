"""S19 extension — substrate gate on the prefix-bind / session resume path (#802).

The original S19 substrate-HTTP-reject (resolution.py pre-PATH-1 gate + PATH 2.8)
only fires when the caller presents a `continuity_token`. A caller holding a
substrate resident's UUID can evade it by resuming via the `agent-{uuid12}`
prefix-bind form (PATH 1) or a PG session row (PATH 2) with NO token. On a
shared-fingerprint localhost host the scope-(a) per-path fingerprint check also
passes, so this is the same-fingerprint co-resident residual from #802.

`_substrate_http_reject` applies the substrate-claims gate to a session-resolved
`agent_uuid`. It is self-scoping (no claim row → no rejection) and fails open.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_modes(monkeypatch):
    monkeypatch.delenv("UNITARES_SESSION_FINGERPRINT_CHECK", raising=False)
    monkeypatch.delenv("UNITARES_PREFIX_BIND_FINGERPRINT", raising=False)
    yield


def _signals(peer_pid=None, fp="ip-x:ua-x"):
    return SimpleNamespace(peer_pid=peer_pid, ip_ua_fingerprint=fp)


def _claim(label="com.unitares.sentinel"):
    return SimpleNamespace(expected_launchd_label=label)


SUBSTRATE_UUID = "f92dcea8-4786-412a-a0eb-362c273382f5"


class TestSubstrateHttpRejectHelper:
    """Direct unit coverage of the four branches of _substrate_http_reject."""

    @pytest.mark.asyncio
    async def test_http_substrate_uuid_is_rejected(self):
        from src.mcp_handlers.identity import resolution as res

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=None),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=_claim()),
        ):
            out = await res._substrate_http_reject(SUBSTRATE_UUID, "unit")

        assert out is not None
        assert out.get("error") == "substrate_anchored_uuid_requires_uds"
        assert out.get("resume_failed") is True
        assert out.get("agent_uuid") == SUBSTRATE_UUID

    @pytest.mark.asyncio
    async def test_uds_peer_pid_present_passes(self):
        """With a kernel-attested peer PID (UDS), the gate defers — returns None."""
        from src.mcp_handlers.identity import resolution as res

        claim_mock = AsyncMock(return_value=_claim())
        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=4321),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim", new=claim_mock
        ):
            out = await res._substrate_http_reject(SUBSTRATE_UUID, "unit")

        assert out is None
        # Short-circuits before even touching the claims table.
        claim_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_substrate_uuid_passes(self):
        from src.mcp_handlers.identity import resolution as res

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=None),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=None),  # no claim row → self-scoping pass
        ):
            out = await res._substrate_http_reject(
                "00000000-1111-2222-3333-444444444444", "unit"
            )

        assert out is None

    @pytest.mark.asyncio
    async def test_fails_open_on_exception(self):
        from src.mcp_handlers.identity import resolution as res

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=None),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            out = await res._substrate_http_reject(SUBSTRATE_UUID, "unit")

        assert out is None  # never breaks resolution


class TestPath1Wiring:
    """The gate is wired into PATH 1 (Redis cache hit / prefix-bind)."""

    @pytest.mark.asyncio
    async def test_path1_substrate_over_http_refused(self):
        from src.mcp_handlers.identity import resolution as res

        cached_payload = {
            "agent_id": SUBSTRATE_UUID,
            "display_agent_id": "Sentinel",
            "bind_ip_ua": "ip-orig:ua-orig",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=None),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=_claim()),
        ):
            result = await res.resolve_session_identity(
                session_key=f"agent-{SUBSTRATE_UUID[:12]}",
                resume=True,
            )

        assert result.get("error") == "substrate_anchored_uuid_requires_uds", (
            f"Substrate UUID prefix-bind over HTTP must be refused. Got: {result}"
        )

    @pytest.mark.asyncio
    async def test_path1_substrate_over_uds_resumes(self):
        from src.mcp_handlers.identity import resolution as res

        cached_payload = {
            "agent_id": SUBSTRATE_UUID,
            "display_agent_id": "Sentinel",
            "bind_ip_ua": "ip-orig:ua-orig",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=9999),  # UDS peer attested
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=_claim()),
        ), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Sentinel"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ):
            result = await res.resolve_session_identity(
                session_key=f"agent-{SUBSTRATE_UUID[:12]}",
                resume=True,
            )

        assert result.get("source") == "redis"
        assert result.get("agent_uuid") == SUBSTRATE_UUID


class TestPath2Wiring:
    """The gate is wired into PATH 2 (PG session row, Redis cache miss)."""

    @pytest.mark.asyncio
    async def test_path2_substrate_over_http_refused(self):
        from src.mcp_handlers.identity import resolution as res

        # Redis miss → fall to PATH 2.
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=None)

        fake_db = MagicMock()
        fake_db.init = AsyncMock(return_value=None)
        fake_db.get_session = AsyncMock(
            return_value=SimpleNamespace(agent_id=SUBSTRATE_UUID)
        )

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution.get_db", return_value=fake_db
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_id_from_metadata",
            new=AsyncMock(return_value="Sentinel_mcp"),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals(peer_pid=None),
        ), patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=_claim()),
        ):
            result = await res.resolve_session_identity(
                session_key=f"agent-{SUBSTRATE_UUID[:12]}",
                resume=True,
            )

        assert result.get("error") == "substrate_anchored_uuid_requires_uds", (
            f"Substrate UUID PG-session resume over HTTP must be refused. Got: {result}"
        )
