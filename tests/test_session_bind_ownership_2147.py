"""#2147: `_perform_session_bind` refuses a destination it cannot show the
caller owns, for every caller.

#2144 closed the one demonstrated path into an unowned bind destination by
guarding two callers. The helper they call was unchanged: it wrote the uuid
onto whatever key it was handed, in Redis, Postgres and the sticky transport
cache, so the next caller to reach it with a foreign key would repeat #2142.

The ownership predicate the safe callers already applied now lives in one
place (`bind_destination_refusal`) and the helper applies it itself. The four
shapes the live call sites produce are pinned here as still succeeding.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.identity import handlers
from src.mcp_handlers.identity.session import (
    FOREIGN_DESTINATION_SOURCES,
    UNDECLARED_DESTINATION_PROVENANCE,
    bind_destination_refusal,
)
from src.mcp_handlers.identity.shared import make_client_session_id
from tests.helpers import parse_result

FOREIGN_KEY = "agent-FOREIGN-PRIOR-ONBOARD"
OWN_TRANSPORT_SOURCES = [
    "mcp_session_id", "x_session_id", "oauth_client_id", "x_client_id",
    "ip_ua_fingerprint", "context_mcp_session_id", "context_session_key",
    "stdio_fallback", "explicit_client_session_id", "continuity_token",
]


@pytest.fixture
def agent_uuid():
    return str(uuid.uuid4())


@pytest.fixture
def writes():
    """Every store the helper writes, mocked and observable. `db` is what
    `get_db()` hands the Postgres step; `transport` is the sticky-cache write."""
    db = AsyncMock()
    db.get_identity.return_value = SimpleNamespace(identity_id="ident-1")
    cache = AsyncMock()
    transport = MagicMock()  # update_transport_binding is synchronous
    with patch.object(handlers, "_cache_session", cache), \
         patch.object(handlers, "get_db", return_value=db), \
         patch("src.mcp_handlers.middleware.identity_step.update_transport_binding", transport), \
         patch("src.mcp_handlers.middleware.identity_step._transport_cache_key", return_value="sticky:fp"), \
         patch("src.mcp_handlers.context.get_session_signals",
               return_value=SimpleNamespace(mcp_session_id=None, ip_ua_fingerprint="fp")):
        yield SimpleNamespace(cache=cache, db=db, transport=transport)


def _nothing_written(w) -> bool:
    return (
        not w.cache.await_args_list
        and not w.db.create_session.await_args_list
        and not w.transport.call_args_list
    )


class TestThePredicate:
    def test_the_agents_own_stable_id_is_owned_whatever_its_source(self, agent_uuid):
        own = make_client_session_id(agent_uuid)
        for source in (None, "pinned_onboard_session", "mcp_session_id"):
            assert bind_destination_refusal(agent_uuid, own, source) is None

    def test_another_agents_stable_id_is_not_owned(self, agent_uuid):
        other = make_client_session_id(str(uuid.uuid4()))
        assert bind_destination_refusal(agent_uuid, other, None) == UNDECLARED_DESTINATION_PROVENANCE

    def test_a_foreign_source_is_refused_by_name(self, agent_uuid):
        assert bind_destination_refusal(agent_uuid, FOREIGN_KEY, "pinned_onboard_session") == "pinned_onboard_session"
        assert "pinned_onboard_session" in FOREIGN_DESTINATION_SOURCES

    def test_an_undeclared_source_fails_closed(self, agent_uuid):
        """The #2142 bind reached the helper with exactly this shape: a key
        the caller did not derive from its own uuid and no provenance."""
        assert bind_destination_refusal(agent_uuid, FOREIGN_KEY, None) == UNDECLARED_DESTINATION_PROVENANCE

    @pytest.mark.parametrize("source", OWN_TRANSPORT_SOURCES)
    def test_a_callers_own_transport_source_is_owned(self, agent_uuid, source):
        assert bind_destination_refusal(agent_uuid, "mcp:some-transport", source) is None

    def test_a_short_uuid_does_not_raise(self):
        """make_client_session_id raises on a malformed uuid; the predicate
        must still answer, and it must not answer 'owned'."""
        assert bind_destination_refusal("short", "agent-short", None) == UNDECLARED_DESTINATION_PROVENANCE


class TestTheHelperRefusesForAnyCaller:
    @pytest.mark.asyncio
    async def test_a_foreign_destination_writes_nothing(self, agent_uuid, writes):
        result = await handlers._perform_session_bind(
            agent_uuid, FOREIGN_KEY, source="some_future_caller",
            key_source="pinned_onboard_session",
        )
        assert result["bound"] is False
        assert result["bind_refused"] == "pinned_onboard_session"
        assert _nothing_written(writes)

    @pytest.mark.asyncio
    async def test_an_undeclared_destination_writes_nothing(self, agent_uuid, writes):
        """A caller that forgets to say where its key came from lands on the
        refusal, not on the #2142 write."""
        result = await handlers._perform_session_bind(agent_uuid, FOREIGN_KEY, source="some_future_caller")
        assert result["bound"] is False
        assert result["bind_refused"] == UNDECLARED_DESTINATION_PROVENANCE
        assert _nothing_written(writes)

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_echo_the_key(self, agent_uuid, writes):
        result = await handlers._perform_session_bind(
            agent_uuid, FOREIGN_KEY, key_source="pinned_onboard_session",
        )
        assert result["session_key"] is None
        assert FOREIGN_KEY[:8] not in repr(result)

    @pytest.mark.asyncio
    async def test_a_refusal_is_the_same_shape_as_a_bind(self, agent_uuid, writes):
        """Callers read `bound`; a refusal must not raise or change type. The
        onboard call site awaits the helper bare, with no try/except."""
        refused = await handlers._perform_session_bind(agent_uuid, FOREIGN_KEY, key_source="pinned_onboard_session")
        bound = await handlers._perform_session_bind(agent_uuid, make_client_session_id(agent_uuid))
        assert isinstance(refused, dict) and isinstance(bound, dict)
        assert set(refused) >= {"bound", "session_key"}
        assert "bind_refused" not in bound


class TestEveryLiveCallSiteStillSucceeds:
    """The three live sites, by the exact shape each hands the helper:
    identity() at source="identity_stable_session", onboard() at
    source="onboard_stable_session", and bind_session()."""

    @pytest.mark.asyncio
    async def test_identity_stable_session(self, agent_uuid, writes):
        own = make_client_session_id(agent_uuid)
        result = await handlers._perform_session_bind(
            agent_uuid=agent_uuid,
            session_key=own,
            display_agent_id="Claude_20260923",
            source="identity_stable_session",
        )
        assert result["bound"] is True
        assert "bind_refused" not in result
        writes.cache.assert_awaited_once_with(own, agent_uuid, display_agent_id="Claude_20260923")
        assert writes.db.create_session.await_count == 1
        assert writes.db.create_session.await_args.kwargs["session_id"] == own

    @pytest.mark.asyncio
    async def test_onboard_stable_session(self, agent_uuid, writes):
        own = make_client_session_id(agent_uuid)
        result = await handlers._perform_session_bind(
            agent_uuid, own, display_agent_id="Claude_20260923", source="onboard_stable_session",
        )
        assert result["bound"] is True
        assert "bind_refused" not in result
        writes.cache.assert_awaited_once_with(own, agent_uuid, display_agent_id="Claude_20260923")
        assert writes.db.create_session.await_args.kwargs["client_info"]["bound_via"] == "onboard_stable_session"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", OWN_TRANSPORT_SOURCES)
    async def test_bind_session_with_the_callers_own_transport_key(self, agent_uuid, writes, source):
        result = await handlers._perform_session_bind(
            agent_uuid, "mcp:my-transport", display_agent_id="Claude_20260923",
            source="bind_session", key_source=source,
        )
        assert result["bound"] is True
        assert "bind_refused" not in result
        writes.cache.assert_awaited_once_with("mcp:my-transport", agent_uuid, display_agent_id="Claude_20260923")
        assert writes.transport.call_count == 1


class TestBindSessionEndToEnd:
    """The REST-to-MCP bridge, through the real helper."""

    def _resolved(self, agent_uuid):
        return {
            "agent_uuid": agent_uuid,
            "agent_id": "Claude_20260923",
            "label": "TestAgent",
            "created": False,
        }

    @pytest.mark.asyncio
    async def test_a_transport_key_is_still_bound(self, agent_uuid, writes):
        with patch.object(handlers, "resolve_session_identity", AsyncMock(return_value=self._resolved(agent_uuid))), \
             patch.object(handlers, "derive_session_key_with_source",
                          AsyncMock(return_value=("mcp:test-session", "mcp_session_id"))):
            data = parse_result(await handlers.handle_bind_session({
                "client_session_id": "agent-abc123",
                "resume": True,
            }))
        assert data["success"] is True
        assert data["bound"] is True
        assert data["agent_uuid"] == agent_uuid
        writes.cache.assert_awaited_once_with("mcp:test-session", agent_uuid, display_agent_id="Claude_20260923")

    @pytest.mark.asyncio
    async def test_a_foreign_destination_is_refused_and_nothing_is_written(self, agent_uuid, writes):
        """#2142's guard in bind_session, now exercised behaviourally, and the
        helper's own refusal behind it: neither store sees the stranger's key."""
        with patch.object(handlers, "resolve_session_identity", AsyncMock(return_value=self._resolved(agent_uuid))), \
             patch.object(handlers, "derive_session_key_with_source",
                          AsyncMock(return_value=(FOREIGN_KEY, "pinned_onboard_session"))):
            data = parse_result(await handlers.handle_bind_session({
                "client_session_id": "agent-abc123",
                "resume": True,
            }))
        assert data["success"] is True
        assert data["bound"] is False
        assert data["rebind_refused"] == "pinned_onboard_session"
        assert data["mcp_session_key"] is None
        assert _nothing_written(writes)

    @pytest.mark.asyncio
    async def test_a_helper_refusal_surfaces_as_a_refusal(self, agent_uuid, writes):
        """If the helper refuses where bind_session's own guard did not, the
        payload must say so rather than claim `bound: True`."""
        async def _refuse(*args, **kwargs):
            return {"bound": False, "session_key": None, "bind_refused": "pinned_onboard_session"}

        with patch.object(handlers, "resolve_session_identity", AsyncMock(return_value=self._resolved(agent_uuid))), \
             patch.object(handlers, "derive_session_key_with_source",
                          AsyncMock(return_value=("mcp:test-session", "mcp_session_id"))), \
             patch.object(handlers, "_perform_session_bind", _refuse):
            data = parse_result(await handlers.handle_bind_session({
                "client_session_id": "agent-abc123",
                "resume": True,
            }))
        assert data["bound"] is False
        assert data["rebind_refused"] == "pinned_onboard_session"
        assert data["mcp_session_key"] is None
