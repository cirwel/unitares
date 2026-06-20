"""Tests for operator-token identity resolution (#425 dashboard-identity decision).

Under STRICT_IDENTITY_REQUIRED the REST gate keys on the RESOLVED context
binding, never on credential presence (council finding, PR #610). These
tests pin the decision's implementation: a valid X-Unitares-Operator token
EARNS a stable resolved identity through the canonical resolver; an absent,
invalid, or rotated-out token leaves the caller unbound; and the operator
binding never leaks into the IP:UA sticky transport cache where same-host
callers without the header could inherit it.
"""

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity.operator import (
    _operator_identity_cache,
    _operator_mint_locks,
    operator_session_key,
    operator_token_fingerprint,
    resolve_operator_identity,
)


@dataclass
class FakeSignals:
    """Minimal SessionSignals stand-in."""
    mcp_session_id: Optional[str] = None
    x_session_id: Optional[str] = None
    x_client_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    ip_ua_fingerprint: Optional[str] = None
    user_agent: Optional[str] = None
    client_hint: Optional[str] = None
    x_agent_name: Optional[str] = None
    x_agent_id: Optional[str] = None
    transport: str = "rest"
    unitares_operator_token: Optional[str] = None


VALID_TOKEN = "op-secret-token-1"


@pytest.fixture(autouse=True)
def clean_operator_cache():
    _operator_identity_cache.clear()
    _operator_mint_locks.clear()
    yield
    _operator_identity_cache.clear()
    _operator_mint_locks.clear()


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", f"{VALID_TOKEN},op-secret-token-2")


def _resumed(uuid="uuid-operator-1"):
    return {"agent_uuid": uuid, "source": "postgres", "created": False}


def _miss():
    return {"resume_failed": True, "error": "session_resolve_miss"}


class TestDefaultDeny:
    @pytest.mark.asyncio
    async def test_no_header_returns_none(self, allowlist):
        assert await resolve_operator_identity(FakeSignals()) is None

    @pytest.mark.asyncio
    async def test_wrong_token_returns_none(self, allowlist):
        signals = FakeSignals(unitares_operator_token="not-on-the-list")
        assert await resolve_operator_identity(signals) is None

    @pytest.mark.asyncio
    async def test_empty_allowlist_returns_none(self, monkeypatch):
        monkeypatch.delenv("UNITARES_OPERATOR_TOKENS", raising=False)
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        assert await resolve_operator_identity(signals) is None

    @pytest.mark.asyncio
    async def test_no_signals_context_returns_none(self, allowlist):
        with patch(
            "src.mcp_handlers.identity.operator.get_session_signals",
            return_value=None,
        ):
            assert await resolve_operator_identity() is None


class TestResolution:
    @pytest.mark.asyncio
    async def test_valid_token_resumes_stable_identity(self, allowlist):
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed(),
        ) as mock_resolve:
            result = await resolve_operator_identity(signals)

        assert result == {
            "agent_uuid": "uuid-operator-1",
            "session_key": operator_session_key(VALID_TOKEN),
            "source": "operator_token",
        }
        mock_resolve.assert_awaited_once()
        kwargs = mock_resolve.await_args.kwargs
        assert kwargs["persist"] is True
        assert kwargs["resume"] is True
        assert kwargs["client_hint"] == "operator"

    @pytest.mark.asyncio
    async def test_first_use_mints_with_spawn_reason(self, allowlist):
        """S21-a MISS on resume → explicit mint with operator_credential."""
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            side_effect=[_miss(), _resumed("uuid-operator-minted")],
        ) as mock_resolve, patch(
            "src.mcp_handlers.identity.operator._persisted_operator_uuid",
            new_callable=AsyncMock,
            # adopt-winner PG readback (#644): row names our mint
            return_value="uuid-operator-minted",
        ):
            result = await resolve_operator_identity(signals)

        assert result["agent_uuid"] == "uuid-operator-minted"
        assert mock_resolve.await_count == 2
        mint_kwargs = mock_resolve.await_args_list[1].kwargs
        assert mint_kwargs["force_new"] is True
        assert mint_kwargs["spawn_reason"] == "operator_credential"
        assert mint_kwargs["persist"] is True

    @pytest.mark.asyncio
    async def test_session_key_is_deterministic_and_opaque(self):
        key = operator_session_key(VALID_TOKEN)
        assert key == operator_session_key(VALID_TOKEN)
        assert key.startswith("operator:")
        assert VALID_TOKEN not in key
        assert len(operator_token_fingerprint(VALID_TOKEN)) == 16

    @pytest.mark.asyncio
    async def test_resolver_no_uuid_returns_none(self, allowlist):
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value={"error": "db_unavailable"},
        ):
            assert await resolve_operator_identity(signals) is None
        assert not _operator_identity_cache


class TestCacheAndRotation:
    @pytest.mark.asyncio
    async def test_second_call_within_ttl_skips_resolver(self, allowlist):
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed(),
        ) as mock_resolve:
            first = await resolve_operator_identity(signals)
            second = await resolve_operator_identity(signals)

        assert first == second
        mock_resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rotation_revokes_despite_warm_cache(self, monkeypatch):
        """Allowlist is checked before the cache — rotating the token out
        revokes immediately even when the identity is still memoized."""
        monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", VALID_TOKEN)
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed(),
        ):
            assert await resolve_operator_identity(signals) is not None

        monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", "different-token")
        assert await resolve_operator_identity(signals) is None

    @pytest.mark.asyncio
    async def test_expired_cache_re_resolves(self, allowlist):
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        fp = operator_token_fingerprint(VALID_TOKEN)
        _operator_identity_cache[fp] = ("uuid-stale", time.monotonic() - 301)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed("uuid-fresh"),
        ) as mock_resolve:
            result = await resolve_operator_identity(signals)
        assert result["agent_uuid"] == "uuid-fresh"
        mock_resolve.assert_awaited_once()


class TestMintRace:
    """First-use single-flight + adopt-winner (issue #644).

    Live 2026-06-12: two dashboard tabs' cold-cache sweep burst minted 5
    operator identities for one token fingerprint — every in-flight request
    took the resume-miss→force_new path before the first session row landed.
    """

    @pytest.mark.asyncio
    async def test_concurrent_first_use_mints_exactly_once(self, allowlist):
        import asyncio

        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        minted = []
        persisted = {"uuid": None}

        async def fake_resolver(session_key, **kwargs):
            # Simulate the real PG semantics: resume misses until a mint
            # persists the row; force_new always mints a fresh UUID but only
            # the FIRST mint's row wins (create_session ON CONFLICT DO
            # NOTHING). The sleep(0) yields the event loop so the gather
            # genuinely interleaves — without it the first task runs to
            # completion uninterrupted and the lock is never contended.
            await asyncio.sleep(0)
            if kwargs.get("force_new"):
                uuid = f"uuid-mint-{len(minted) + 1}"
                minted.append(uuid)
                if persisted["uuid"] is None:
                    persisted["uuid"] = uuid
                return {"agent_uuid": uuid, "source": "created", "created": True}
            if persisted["uuid"] is not None:
                return _resumed(persisted["uuid"])
            return _miss()

        async def fake_pg_readback(session_key):
            await asyncio.sleep(0)
            return persisted["uuid"]

        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new=fake_resolver,
        ), patch(
            "src.mcp_handlers.identity.operator._persisted_operator_uuid",
            new=fake_pg_readback,
        ):
            results = await asyncio.gather(
                *[resolve_operator_identity(signals) for _ in range(10)]
            )

        assert len(minted) == 1, f"expected single-flight mint, got {minted}"
        uuids = {r["agent_uuid"] for r in results}
        assert uuids == {persisted["uuid"]}

    @pytest.mark.asyncio
    async def test_lost_persist_race_adopts_winner(self, allowlist):
        """If our session-row insert lost (second process / pre-lock burst),
        the post-mint PG readback adopts the persisted identity, not our
        ghost. The readback must bypass the resolver entirely — PATH 1
        (Redis) holds our own racing mint at this point."""
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            side_effect=[
                _miss(),                     # initial resume: no row yet
                _resumed("uuid-our-ghost"),  # our mint
            ],
        ), patch(
            "src.mcp_handlers.identity.operator._persisted_operator_uuid",
            new_callable=AsyncMock,
            return_value="uuid-the-winner",  # PG row: another writer won
        ):
            result = await resolve_operator_identity(signals)

        assert result["agent_uuid"] == "uuid-the-winner"
        fp = operator_token_fingerprint(VALID_TOKEN)
        assert _operator_identity_cache[fp][0] == "uuid-the-winner"

    @pytest.mark.asyncio
    async def test_pg_readback_failure_keeps_our_mint(self, allowlist):
        """Adopt-winner is best-effort: a failed PG readback keeps the
        minted identity rather than degrading to unbound."""
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            side_effect=[_miss(), _resumed("uuid-our-mint")],
        ), patch(
            "src.mcp_handlers.identity.operator._persisted_operator_uuid",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_operator_identity(signals)

        assert result["agent_uuid"] == "uuid-our-mint"


class TestRestPrebindIntegration:
    """The REST prebind path: operator binding wins, sticky cache untouched."""

    @pytest.mark.asyncio
    async def test_operator_binding_set_and_sticky_cache_clean(self, allowlist):
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.context import get_session_resolution_source
        from src.mcp_handlers.middleware.identity_step import (
            _transport_identity_cache,
        )

        _transport_identity_cache.clear()
        signals = FakeSignals(
            ip_ua_fingerprint="9.9.9.1:uaOP",
            unitares_operator_token=VALID_TOKEN,
        )
        arguments: dict = {}
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed(),
        ):
            result = await _resolve_http_bound_agent(
                "archive_agent", arguments, signals
            )

        assert result == "uuid-operator-1"
        assert arguments["agent_id"] == "uuid-operator-1"
        assert get_session_resolution_source() == "operator_token"
        # The operator binding must NOT be cached under the IP:UA
        # fingerprint — same-host callers without the header would
        # inherit it.
        assert not _transport_identity_cache

    @pytest.mark.asyncio
    async def test_operator_beats_sticky_cache(self, allowlist):
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.middleware.identity_step import (
            _transport_identity_cache,
            update_transport_binding,
        )

        _transport_identity_cache.clear()
        update_transport_binding("sticky:9.9.9.2:uaOP", "uuid-fingerprint", "sk", "rest")
        signals = FakeSignals(
            ip_ua_fingerprint="9.9.9.2:uaOP",
            unitares_operator_token=VALID_TOKEN,
        )
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed("uuid-operator-2"),
        ):
            result = await _resolve_http_bound_agent("config", {}, signals)
        assert result == "uuid-operator-2"
        _transport_identity_cache.clear()

    @pytest.mark.asyncio
    async def test_resolver_error_degrades_to_unbound(self, allowlist):
        """Valid token + resolver failure → unbound (visible refusal under
        strict), never a silent bypass."""
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.context import update_context_agent_id

        update_context_agent_id(None)
        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        with patch(
            "src.mcp_handlers.identity.operator.resolve_operator_identity",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ), patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="sk-op-err",
        ), patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value={"resume_failed": True, "error": "session_resolve_miss"},
        ):
            result = await _resolve_http_bound_agent("archive_agent", {}, signals)
        assert result is None

    @pytest.mark.asyncio
    async def test_pre_onboard_read_no_proof_skips_resolution(self):
        """#945 §1 (REST parity): a pre_onboard read with no proof must not
        fingerprint-resolve — that would lazily bind + sticky-cache an identity
        for a pure read. get_governance_metrics is the case the stale skip_tools
        set missed; the get_call_identity_requirement check now covers it."""
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.middleware.identity_step import (
            _transport_identity_cache,
        )

        _transport_identity_cache.clear()
        resolve_mock = AsyncMock(return_value=_resumed("uuid-should-not-bind"))
        signals = FakeSignals(ip_ua_fingerprint="9.9.9.9:uaRO")
        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="sk-readonly",
        ), patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            resolve_mock,
        ):
            result = await _resolve_http_bound_agent(
                "get_governance_metrics", {}, signals
            )

        assert result is None
        assert resolve_mock.await_count == 0
        # Nothing cached from a pure read.
        assert not _transport_identity_cache


class TestStrictGateEndToEnd:
    """Under STRICT_IDENTITY_REQUIRED: operator-resolved binding passes the
    REST gate; absent credential still refuses."""

    @pytest.mark.asyncio
    async def test_write_refuses_without_credential(self, monkeypatch, allowlist):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        from src.mcp_handlers.context import update_context_agent_id
        from src.services.http_tool_service import _strict_identity_refusal_or_none

        update_context_agent_id(None)
        refusal = _strict_identity_refusal_or_none("archive_agent", {})
        assert refusal is not None
        assert refusal["status"] == "identity_required"

    @pytest.mark.asyncio
    async def test_write_passes_with_operator_binding(self, monkeypatch, allowlist):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        from src.http_api import _resolve_http_bound_agent
        from src.services.http_tool_service import _strict_identity_refusal_or_none

        signals = FakeSignals(unitares_operator_token=VALID_TOKEN)
        arguments: dict = {}
        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=_resumed(),
        ):
            bound = await _resolve_http_bound_agent(
                "archive_agent", arguments, signals
            )
        assert bound == "uuid-operator-1"
        assert _strict_identity_refusal_or_none("archive_agent", arguments) is None


class TestAssuranceTier:
    def test_operator_token_is_strong(self):
        from src.mcp_handlers.updates.phases import _compute_identity_assurance

        assurance = _compute_identity_assurance("operator_token", None)
        assert assurance["tier"] == "strong"
