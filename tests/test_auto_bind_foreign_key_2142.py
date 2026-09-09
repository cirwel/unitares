"""#2142 / #2130: an auxiliary derive must not pick a foreign destination, and
must not restate the request's identity provenance.

Two defects on the same line of code. `handle_identity_adapter` re-derived a
session key with NO arguments, so the ladder fell through to the onboard-pin
lookup, which returns a stored client_session_id WITHOUT comparing it to the
calling agent (`session.py` step 7). The pin is keyed on the User-Agent alone
(`_extract_base_fingerprint`: "we pin by UA_hash ONLY"), so unrelated callers
share the slot.

The guard was `mcp_key != base_session_key` — which passes PRECISELY BECAUSE a
foreign key differs. It then bound this agent's uuid onto that key through a
helper with no ownership validation (#2142). Separately, the same derive
re-stamped the request's proof origin from a lookup that never saw the caller's
arguments (#2130).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from src.mcp_handlers.context import (
    get_session_proof_origin,
    get_session_resolution_source,
    set_session_proof_origin,
    set_session_resolution_source,
)
from src.mcp_handlers.identity import session as S
from src.mcp_handlers.identity.session import (
    FOREIGN_DESTINATION_SOURCES,
    derive_session_key,
    derive_session_key_with_source,
)

FOREIGN_KEY = "agent-FOREIGN-PRIOR-ONBOARD"


class _Signals:
    """Transport signals with no session identifiers — the shape that makes the
    ladder fall through to step 7's pin lookup."""

    mcp_session_id = None
    x_session_id = None
    oauth_client_id = None
    x_client_id = None
    user_agent = "claude-ai/1.0"
    client_hint = None
    ip_ua_fingerprint = "203.0.113.7:d20c2f"


@pytest.fixture
def foreign_pin():
    async def _pin(candidate):
        return FOREIGN_KEY

    with patch.object(S, "lookup_onboard_pin", _pin):
        yield


@pytest.fixture(autouse=True)
def _clean_stamps():
    set_session_resolution_source(None)
    set_session_proof_origin(None)
    yield
    set_session_resolution_source(None)
    set_session_proof_origin(None)


class TestAuxiliaryDeriveDoesNotStamp:
    @pytest.mark.asyncio
    async def test_an_auxiliary_derive_leaves_the_real_provenance_intact(self, foreign_pin):
        """#2130. The load-bearing derive is the single stamper."""
        await derive_session_key(_Signals(), {"client_session_id": "agent-MINE-1234"})
        assert get_session_resolution_source() == "explicit_client_session_id"
        assert get_session_proof_origin() == "caller_asserted"

        await derive_session_key(_Signals(), stamp=False)

        assert get_session_resolution_source() == "explicit_client_session_id"
        assert get_session_proof_origin() == "caller_asserted"

    @pytest.mark.asyncio
    async def test_stamping_is_the_default(self, foreign_pin):
        """A caller that forgets the flag gets the OLD behaviour, which is
        fail-closed. Defaulting to False would leave proof_origin unset, and
        `updates/phases.py` passes an unset origin through by design."""
        await derive_session_key(_Signals(), {"client_session_id": "agent-MINE-1234"})
        await derive_session_key(_Signals())
        assert get_session_proof_origin() == "server_inferred"

    @pytest.mark.asyncio
    async def test_the_key_is_identical_with_and_without_stamping(self, foreign_pin):
        """stamp only suppresses a side effect; it must not change resolution."""
        stamped = await derive_session_key(_Signals())
        set_session_resolution_source(None)
        set_session_proof_origin(None)
        unstamped = await derive_session_key(_Signals(), stamp=False)
        assert stamped == unstamped == FOREIGN_KEY


class TestSourceIsReportedOutOfBand:
    @pytest.mark.asyncio
    async def test_with_source_reports_the_winning_source(self, foreign_pin):
        key, source = await derive_session_key_with_source(_Signals(), stamp=False)
        assert key == FOREIGN_KEY
        assert source == "pinned_onboard_session"

    @pytest.mark.asyncio
    async def test_the_source_is_readable_even_though_nothing_was_stamped(self, foreign_pin):
        """The reason `with_source` exists: under stamp=False the contextvar
        still holds the PREVIOUS derivation's source, so a caller that read it
        would decide on the wrong value."""
        set_session_resolution_source("explicit_client_session_id")
        _, source = await derive_session_key_with_source(_Signals(), stamp=False)
        assert source == "pinned_onboard_session"
        assert get_session_resolution_source() == "explicit_client_session_id"

    @pytest.mark.asyncio
    async def test_derive_session_key_always_returns_a_bare_string(self, foreign_pin):
        """Regression guard. An earlier revision made the return type
        conditional on a flag; a test that mocks this function to return a
        string then had its characters unpacked instead of failing loudly.
        The type must not depend on an argument."""
        for kwargs in ({}, {"stamp": False}, {"stamp": True}):
            assert isinstance(await derive_session_key(_Signals(), **kwargs), str)


class TestForeignDestination:
    def test_the_pin_source_is_classified_foreign(self):
        assert "pinned_onboard_session" in FOREIGN_DESTINATION_SOURCES

    @pytest.mark.parametrize("source", [
        "mcp_session_id", "x_session_id", "oauth_client_id", "x_client_id",
        "ip_ua_fingerprint", "stdio_fallback",
    ])
    def test_a_callers_own_transport_sources_are_not_foreign(self, source):
        """Only a store lookup keyed on a shared fingerprint yields someone
        else's key. Every other ladder source names this caller's own transport."""
        assert source not in FOREIGN_DESTINATION_SOURCES

    @pytest.mark.asyncio
    async def test_the_inequality_guard_passes_exactly_when_the_key_is_foreign(self, foreign_pin):
        """The heart of #2142: the old guard was `mcp_key != base_session_key`,
        so it admitted the destination precisely because it belonged to someone
        else. This pins that reasoning so the guard is never restored alone."""
        base = await derive_session_key(
            _Signals(), {"client_session_id": "agent-MINE-1234"}
        )
        mcp_key, source = await derive_session_key_with_source(_Signals(), stamp=False)
        assert mcp_key != base                       # old guard would ADMIT
        assert source in FOREIGN_DESTINATION_SOURCES  # new guard REFUSES


class TestTheUnsafeAutoBindIsGone:
    def test_identity_no_longer_binds_a_re_derived_transport_key(self):
        """#2142: the secondary auto-bind block is removed. The stable-session
        bind, which binds a key derived from the agent's own uuid, stays."""
        import inspect

        from src.mcp_handlers.identity import handlers

        src = inspect.getsource(handlers.handle_identity_adapter)
        assert 'source="identity_auto_bind"' not in src
        assert 'source="identity_stable_session"' in src

    def test_bind_session_refuses_a_foreign_destination(self):
        import inspect

        from src.mcp_handlers.identity import handlers

        src = inspect.getsource(handlers.handle_bind_session)
        assert "FOREIGN_DESTINATION_SOURCES" in src
        # The derive that picks the destination must not stamp.
        assert "stamp=False" in src and "derive_session_key_with_source" in src
