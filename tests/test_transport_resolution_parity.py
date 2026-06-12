"""Golden-parity tests for the single-sourced sticky-cache consult.

PR #245 consolidated the middleware↔handler identity-resolution paths
(S21-b items 5+6); the REST prebind path (`_resolve_http_bound_agent`)
remained a third, hand-mirrored copy that had already drifted (no Redis
restart-recovery, no explicit-UUID guard). These tests pin the shared
consult (`consult_sticky_binding`) and the shared S3 decay envelope
(`sticky_resolution_source`) so the two transports cannot drift again,
and make the one deliberate divergence — REST consults in-memory only —
an explicit, tested parameter instead of a silent difference.
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

from src.mcp_handlers.middleware.identity_step import (
    _transport_identity_cache,
    consult_sticky_binding,
    sticky_resolution_source,
    update_transport_binding,
)


@dataclass
class FakeSignals:
    """Minimal SessionSignals stand-in (mirrors test_sticky_identity.py)."""
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


@pytest.fixture(autouse=True)
def clean_cache():
    _transport_identity_cache.clear()
    yield
    _transport_identity_cache.clear()


def _signals(fp: str = "10.0.0.1:uaP") -> FakeSignals:
    return FakeSignals(ip_ua_fingerprint=fp)


class TestConsultParity:
    """Same signals through the middleware shape and the REST shape must
    reach the same binding decision."""

    @pytest.mark.asyncio
    async def test_hit_parity_between_transport_shapes(self):
        update_transport_binding("sticky:10.0.0.1:uaP", "uuid-parity", "sk-p", "redis")

        mw = await consult_sticky_binding(_signals(), {})  # middleware shape
        rest = await consult_sticky_binding(_signals(), {}, redis_recovery=False)

        assert mw.transport_key == rest.transport_key == "sticky:10.0.0.1:uaP"
        assert mw.binding is not None and rest.binding is not None
        assert mw.binding.agent_uuid == rest.binding.agent_uuid == "uuid-parity"
        assert mw.cacheable and rest.cacheable

    @pytest.mark.asyncio
    async def test_envelope_parity(self):
        """Both surfaces must emit the identical S3 decay envelope."""
        update_transport_binding(
            "sticky:10.0.0.1:uaP", "uuid-parity", "sk-p", "redis",
            original_session_source="x_session_id",
        )
        mw = await consult_sticky_binding(_signals(), {})
        rest = await consult_sticky_binding(_signals(), {}, redis_recovery=False)
        assert (
            sticky_resolution_source(mw.binding)
            == sticky_resolution_source(rest.binding)
            == "sticky_cache:x_session_id"
        )

    @pytest.mark.asyncio
    async def test_envelope_default_unknown(self):
        update_transport_binding("sticky:10.0.0.1:uaP", "uuid-parity", "sk-p", "redis")
        consult = await consult_sticky_binding(_signals(), {})
        assert sticky_resolution_source(consult.binding) == "sticky_cache:unknown"


class TestConsultGuard:
    """Proof-carrying requests suppress the cache hit on both shapes; the
    transport key is still computed for callers that need it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocker", [
        {"force_new": True},
        {"client_session_id": "csi-1"},
        {"continuity_token": "v1.x.y"},
    ])
    async def test_argument_blockers(self, blocker):
        update_transport_binding("sticky:10.0.0.1:uaP", "uuid-cached", "sk", "redis")
        for redis_recovery in (True, False):
            consult = await consult_sticky_binding(
                _signals(), dict(blocker), redis_recovery=redis_recovery
            )
            assert consult.binding is None
            assert consult.cacheable is False
            assert consult.transport_key == "sticky:10.0.0.1:uaP"

    @pytest.mark.asyncio
    async def test_explicit_uuid_blocks(self):
        update_transport_binding("sticky:10.0.0.1:uaP", "uuid-cached", "sk", "redis")
        consult = await consult_sticky_binding(_signals(), {}, has_explicit_uuid=True)
        assert consult.binding is None
        assert consult.cacheable is False

    @pytest.mark.asyncio
    async def test_uncacheable_transport_yields_no_key(self):
        consult = await consult_sticky_binding(
            FakeSignals(x_session_id="stable-id"), {}
        )
        assert consult.transport_key is None
        assert consult.binding is None
        assert consult.cacheable is False

    @pytest.mark.asyncio
    async def test_ttl_expired_binding_misses(self):
        update_transport_binding("sticky:10.0.0.1:uaP", "uuid-stale", "sk", "redis")
        _transport_identity_cache["sticky:10.0.0.1:uaP"].bound_at = (
            time.monotonic() - 7201
        )
        consult = await consult_sticky_binding(_signals(), {}, redis_recovery=False)
        assert consult.binding is None
        assert consult.cacheable is True  # guard passed; entry just expired


class TestRedisRecoveryDivergence:
    """The one deliberate transport divergence: middleware recovers from
    Redis on in-memory miss, REST does not."""

    @pytest.mark.asyncio
    async def test_rest_shape_never_touches_redis(self):
        with patch(
            "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
            new_callable=AsyncMock,
        ) as mock_load:
            consult = await consult_sticky_binding(
                _signals(), {}, redis_recovery=False
            )
        mock_load.assert_not_awaited()
        assert consult.binding is None

    @pytest.mark.asyncio
    async def test_middleware_shape_recovers_from_redis(self):
        from src.mcp_handlers.middleware.identity_step import TransportBinding
        recovered = TransportBinding(
            agent_uuid="uuid-from-redis",
            session_key="sk-r",
            bound_at=time.monotonic(),
            source="redis_recovery",
        )
        with patch(
            "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
            new_callable=AsyncMock,
            return_value=recovered,
        ) as mock_load:
            consult = await consult_sticky_binding(_signals(), {})
        mock_load.assert_awaited_once_with("sticky:10.0.0.1:uaP")
        assert consult.binding is recovered


class TestRestSurfaceParity:
    """End-to-end: the REST prebind emits the same envelope and uuid the
    middleware would for the same cached binding."""

    @pytest.mark.asyncio
    async def test_rest_cache_hit_uses_shared_envelope(self):
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.context import get_session_resolution_source

        update_transport_binding(
            "sticky:10.0.0.2:uaQ", "uuid-rest-q", "sk-q", "rest",
            original_session_source="x_session_id",
        )
        signals = FakeSignals(ip_ua_fingerprint="10.0.0.2:uaQ")

        result = await _resolve_http_bound_agent("call_model", {}, signals)

        assert result == "uuid-rest-q"
        assert get_session_resolution_source() == "sticky_cache:x_session_id"

    @pytest.mark.asyncio
    async def test_rest_writeback_skipped_for_proof_carrying_request(self):
        """client_session_id requests resolve but never cache under the
        bare fingerprint (pre-consolidation REST behavior, preserved)."""
        from src.http_api import _resolve_http_bound_agent

        signals = FakeSignals(ip_ua_fingerprint="10.0.0.3:uaR")
        mock_identity = {"agent_uuid": "uuid-proof", "created": False, "source": "redis"}
        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="sk-proof",
        ), patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            return_value=mock_identity,
        ):
            result = await _resolve_http_bound_agent(
                "call_model", {"client_session_id": "csi-x"}, signals
            )

        assert result == "uuid-proof"
        assert "sticky:10.0.0.3:uaR" not in _transport_identity_cache
