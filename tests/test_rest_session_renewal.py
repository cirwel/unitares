"""REST session-TTL renewal parity (F1, Phase-2 cutover evidence 2026-07-04).

The dispatch middleware renews the durable core.sessions row after every
successful identity resolution (identity_step's post-resolution TTL update).
The REST surface (/v1/tools/call -> _resolve_http_bound_agent) never did:
a REST-only client checking in every 180s left last_active frozen at
provisioning time, so expires_at lapsed after SESSION_TTL_HOURS and the
Redis-loss self-heal path (PATH2, which reads the PG row) silently died
while check-ins kept landing via the Redis binding. Observed live: 440
successful REST check-ins with the PG row untouched.

These tests pin the parity touch: renewal fires exactly on the
successful-resolution branch — not on sticky-cache hits (server-inferred,
no per-call proof), not on skip_tools, not for freshly created identities —
and a renewal failure never fails the call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.http_api import _resolve_http_bound_agent

AGENT_UUID = "a00e9d21-0000-4000-8000-000000000000"
SESSION_KEY = "agent-a00e9d21-cd9"


def _no_consult():
    return SimpleNamespace(binding=None, cacheable=False, transport_key=None)


class _Env:
    """Patch stack for the function-local imports in _resolve_http_bound_agent."""

    def __init__(self):
        self.db = MagicMock()
        self.db.update_session_activity = AsyncMock(return_value=True)
        self.resolve = AsyncMock(
            return_value={"agent_uuid": AGENT_UUID, "created": False, "source": "session_binding"}
        )
        self.consult = AsyncMock(return_value=_no_consult())

    def patches(self):
        return [
            patch(
                "src.mcp_handlers.identity.operator.resolve_operator_identity",
                AsyncMock(return_value=None),
            ),
            patch(
                "src.mcp_handlers.middleware.identity_step.consult_sticky_binding",
                self.consult,
            ),
            patch(
                "src.mcp_handlers.identity.handlers.derive_session_key",
                AsyncMock(return_value=SESSION_KEY),
            ),
            patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity",
                self.resolve,
            ),
            patch(
                "src.mcp_handlers.decorators.get_call_identity_requirement",
                MagicMock(return_value="post_onboard"),
            ),
            patch("src.db.get_db", MagicMock(return_value=self.db)),
        ]


@pytest.fixture
def env():
    e = _Env()
    for p in e.patches():
        p.start()
    try:
        yield e
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_successful_resolution_renews_session_ttl(env):
    agent = await _resolve_http_bound_agent(
        "process_agent_update", {"client_session_id": SESSION_KEY}, MagicMock()
    )
    assert agent == AGENT_UUID
    env.db.update_session_activity.assert_awaited_once_with(SESSION_KEY)


@pytest.mark.asyncio
async def test_created_identity_does_not_renew(env):
    env.resolve.return_value = {"agent_uuid": AGENT_UUID, "created": True}
    agent = await _resolve_http_bound_agent(
        "process_agent_update", {"client_session_id": SESSION_KEY}, MagicMock()
    )
    assert agent is None
    env.db.update_session_activity.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_failure_is_nonfatal(env):
    env.db.update_session_activity = AsyncMock(side_effect=RuntimeError("pool down"))
    agent = await _resolve_http_bound_agent(
        "process_agent_update", {"client_session_id": SESSION_KEY}, MagicMock()
    )
    assert agent == AGENT_UUID
    assert env.db.update_session_activity.await_count == 2  # both attempts consumed


@pytest.mark.asyncio
async def test_renewal_retries_once_then_succeeds(env):
    env.db.update_session_activity = AsyncMock(side_effect=[RuntimeError("blip"), True])
    agent = await _resolve_http_bound_agent(
        "process_agent_update", {"client_session_id": SESSION_KEY}, MagicMock()
    )
    assert agent == AGENT_UUID
    assert env.db.update_session_activity.await_count == 2


@pytest.mark.asyncio
async def test_sticky_cache_hit_does_not_renew(env):
    cached = SimpleNamespace(agent_uuid=AGENT_UUID)
    env.consult.return_value = SimpleNamespace(
        binding=cached, cacheable=False, transport_key=None
    )
    with patch(
        "src.mcp_handlers.middleware.identity_step.sticky_resolution_source",
        MagicMock(return_value="sticky_cache:unknown"),
    ):
        agent = await _resolve_http_bound_agent(
            "process_agent_update", {}, MagicMock()
        )
    assert agent == AGENT_UUID
    env.db.update_session_activity.assert_not_awaited()
    env.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_tools_do_not_renew(env):
    agent = await _resolve_http_bound_agent(
        "identity", {"client_session_id": SESSION_KEY}, MagicMock()
    )
    assert agent is None
    env.db.update_session_activity.assert_not_awaited()
