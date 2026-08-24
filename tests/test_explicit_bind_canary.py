"""Corroboration gate for the explicit-``agent_id`` REST bind path.

`_bind_explicit_http_agent` used to accept an identity on a *shape* check
alone — 36 characters, 4 hyphens — with no lookup and no proof of ownership,
and it ran first in `_resolve_http_prebind`. A resolution that succeeded
there meant the strict-identity gate downstream never saw a miss and never
emitted its typed refusal. That was the surface the 2026-08-05 trust-anchor
audit scoped.

PR #1566 (2026-08-10) added `_explicit_bind_corroboration` as an
observation-only canary — classify, log, change nothing — per the fleet
rule that a gate needs measured data before it is armed. #1565 fixed the
one caller (sentinel) measured relying on the bare-uuid path. Live data at
arming time read 298,573 explicit-bind calls since the prior log rotation,
100% `corroboration=csid`, 0% `none` — but that reading turned out to be
constructed, not measured, through two rounds of review before this shipped:

1. A codex review of the first draft caught that `client_session_id`
   PRESENCE alone was being counted as corroboration, and
   `_inject_http_client_session` (http_routes/tools.py) synthesizes one
   into EVERY REST call that doesn't already carry one — the armed gate
   would have been a no-op for the exact traffic it exists to gate. Fixed
   by keying on `get_csid_transport_injected()` instead of presence.
2. A second codex review, after that fix, caught that CSID corroboration
   still only proved the caller completed *some* onboard — not that the
   onboard was for the ``agent_id`` in THIS call. An attacker's own real
   session plus `agent_id=<victim uuid>` would otherwise still corroborate:
   a confused-deputy bypass one level up from the (also-fixed)
   `continuity_token` gap, and just as real, since a caller always has
   cheap access to a real session of their own. Fixed by resolving the
   session (`resolve_session_identity`) and requiring the SAME agent_uuid.
3. A third codex review, after THAT fix, caught two more gaps in the same
   csid ownership check: (a) the canonical `agent-{uuid[:12]}` session-id
   form is a pure function of the public uuid (`make_client_session_id`,
   `identity/shared.py`) — a caller who merely knows the uuid can compute
   it and pass the equality check with no session of their own, the exact
   prefix-bind hijack a prior KG finding and #802 already named; (b) a
   FAILED resolution (`_substrate_http_reject`'s `resume_failed=True`
   refusal, returned for a substrate resident resumed over HTTP) still
   carries the rejected uuid, and the equality check alone read that as a
   match. Fixed by excluding the canonical form from corroboration credit
   and requiring the absence of `resume_failed`/`error`. Also added: a
   bounded timeout on the new session lookup, since it is the first
   non-trivial I/O this function performs and runs on every REST call
   naming an explicit agent_id.

These tests pin six things:

1. the classifier still grades corroboration correctly;
2. `csid`/`token` corroboration for THIS agent_id still binds — the gate
   must not regress the legitimate case it was designed to keep working;
3. `none` corroboration is now refused (falls through to the next
   resolution step) instead of binding on the caller's word alone;
4. a transport-injected `client_session_id` does NOT count as corroboration
   — only a caller-asserted one does;
5. a real session or token for a DIFFERENT agent does NOT corroborate a
   claim to bind as THIS agent_id;
6. the canonical uuid-derivable session-id form, a failed/refused
   resolution, and a stalled lookup do NOT corroborate either — proof of
   possession is not knowledge of the uuid, a refusal is not a match, and
   an unresponsive dependency degrades safely instead of hanging.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.http_api import _bind_explicit_http_agent, _explicit_bind_corroboration
from src.mcp_handlers.identity.session import create_continuity_token
from src.mcp_handlers.identity.shared import make_client_session_id

VALID = "00000000-0000-0000-0000-000000000000"
OTHER = "11111111-1111-1111-1111-111111111111"


def _mint_token(agent_uuid: str, client_session_id: str = "sid-for-test") -> str:
    """A real, HMAC-signed continuity token naming ``agent_uuid`` — not a
    string constant, because the corroboration gate must reject anything
    that isn't (see TestCorroborationClassifier's token cases)."""
    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(agent_uuid, client_session_id)
    assert token, "create_continuity_token returned None — secret not picked up"
    return token


def _resolved_session(
    agent_uuid: str | None,
    *,
    created: bool = False,
    resume_failed: bool = False,
    error: str | None = None,
    side_effect=None,
):
    """Patch the resolver `_explicit_bind_corroboration` calls to resolve a
    caller-asserted client_session_id to a given agent_uuid (or to nothing,
    for a session that resolves to no one / was just freshly minted /
    refused)."""
    if side_effect is not None:
        return patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
            side_effect=side_effect,
        )
    if agent_uuid is None:
        value = None
    else:
        value = {"agent_uuid": agent_uuid, "created": created}
        if resume_failed:
            value["resume_failed"] = True
        if error:
            value["error"] = error
    return patch(
        "src.mcp_handlers.identity.handlers.resolve_session_identity",
        new_callable=AsyncMock,
        return_value=value,
    )


def _not_injected():
    """The caller-asserted case: client_session_id was NOT transport-injected."""
    from src.mcp_handlers.context import set_csid_transport_injected

    return _FlagContext(set_csid_transport_injected)


class _FlagContext:
    def __init__(self, setter):
        self._setter = setter

    def __enter__(self):
        self._setter(False)
        return self

    def __exit__(self, *exc):
        self._setter(False)


class TestCorroborationClassifier:
    @pytest.mark.asyncio
    async def test_uuid_alone_is_none(self):
        # The population a corroboration requirement would turn away.
        assert await _explicit_bind_corroboration({"agent_id": VALID}) == "none"

    @pytest.mark.asyncio
    async def test_session_resolving_to_this_agent_is_csid(self):
        # The server issued this, the caller demonstrably onboarded, AND the
        # onboard was for the SAME agent_id being claimed.
        with _not_injected(), _resolved_session(VALID):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "csid"
            )

    @pytest.mark.asyncio
    async def test_session_resolving_to_a_different_agent_does_not_count(self):
        # The confused-deputy case a second codex review caught: a REAL,
        # caller-asserted session — just not for THIS agent_id.
        with _not_injected(), _resolved_session(OTHER):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "none"
            )

    @pytest.mark.asyncio
    async def test_session_that_resolves_to_nobody_does_not_count(self):
        with _not_injected(), _resolved_session(None):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "none"
            )

    @pytest.mark.asyncio
    async def test_freshly_minted_session_does_not_count(self):
        # created=True means resolve_session_identity manufactured a NEW
        # identity for this session key rather than finding an existing
        # binding — that is not proof of anything, let alone of VALID.
        with _not_injected(), _resolved_session(VALID, created=True):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "none"
            )

    @pytest.mark.asyncio
    async def test_verified_continuity_token_for_this_agent_is_token(self):
        token = _mint_token(VALID)
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            assert (
                await _explicit_bind_corroboration({"agent_id": VALID, "continuity_token": token})
                == "token"
            )

    @pytest.mark.asyncio
    async def test_unverifiable_continuity_token_does_not_count(self):
        # A caller-suppliable string with no shape requirement is exactly as
        # forgeable as a bare uuid — the same trap one level down. This is
        # the case a codex review caught: the original classifier credited
        # ANY non-empty string.
        assert (
            await _explicit_bind_corroboration({"agent_id": VALID, "continuity_token": "garbage"})
            == "none"
        )

    @pytest.mark.asyncio
    async def test_valid_token_for_a_different_agent_does_not_count(self):
        # A token that verifies but names OTHER must not corroborate a claim
        # to bind as VALID — else a caller corroborates agent A's uuid with
        # a valid token for agent B.
        token = _mint_token(OTHER)
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            assert (
                await _explicit_bind_corroboration({"agent_id": VALID, "continuity_token": token})
                == "none"
            )

    @pytest.mark.asyncio
    async def test_csid_wins_when_both_present(self):
        token = _mint_token(VALID)
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            with _not_injected(), _resolved_session(VALID):
                args = {"agent_id": VALID, "client_session_id": "c", "continuity_token": token}
                assert await _explicit_bind_corroboration(args) == "csid"

    @pytest.mark.asyncio
    async def test_empty_values_do_not_count_as_corroboration(self):
        # "" is not proof. Counting it would understate the population that
        # breaks when the path is tightened — the exact number this exists for.
        assert await _explicit_bind_corroboration({"client_session_id": ""}) == "none"
        assert await _explicit_bind_corroboration({"continuity_token": ""}) == "none"

    @pytest.mark.asyncio
    async def test_non_string_values_do_not_count(self):
        assert await _explicit_bind_corroboration({"client_session_id": 123}) == "none"

    @pytest.mark.asyncio
    async def test_non_dict_is_none(self):
        assert await _explicit_bind_corroboration(None) == "none"
        assert await _explicit_bind_corroboration("nope") == "none"


class TestBindRequiresCorroboration:
    """The gate binds on proof. A bare uuid is no longer proof."""

    @pytest.mark.asyncio
    async def test_csid_corroborated_uuid_still_binds(self):
        with _not_injected(), _resolved_session(VALID):
            assert (
                await _bind_explicit_http_agent({"agent_id": VALID, "client_session_id": "c"})
                == VALID
            )

    @pytest.mark.asyncio
    async def test_csid_for_a_different_agent_is_refused(self):
        with _not_injected(), _resolved_session(OTHER):
            assert (
                await _bind_explicit_http_agent({"agent_id": VALID, "client_session_id": "c"})
                is None
            )

    @pytest.mark.asyncio
    async def test_token_corroborated_uuid_still_binds(self):
        token = _mint_token(VALID)
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            assert (
                await _bind_explicit_http_agent({"agent_id": VALID, "continuity_token": token})
                == VALID
            )

    @pytest.mark.asyncio
    async def test_unverifiable_token_is_now_refused(self):
        assert (
            await _bind_explicit_http_agent({"agent_id": VALID, "continuity_token": "garbage"})
            is None
        )

    @pytest.mark.asyncio
    async def test_uuid_alone_is_now_refused(self):
        # This is the case the audit is about. It must now fall through
        # (None) instead of binding on the caller's word alone — the exact
        # behaviour change the canary in #1566 measured the cost of before
        # this armed.
        assert await _bind_explicit_http_agent({"agent_id": VALID}) is None

    @pytest.mark.asyncio
    async def test_wrong_length_still_rejected(self):
        assert await _bind_explicit_http_agent({"agent_id": "too-short"}) is None

    @pytest.mark.asyncio
    async def test_wrong_hyphen_count_still_rejected(self):
        assert await _bind_explicit_http_agent({"agent_id": "0" * 36}) is None

    @pytest.mark.asyncio
    async def test_missing_agent_id_still_returns_none(self):
        assert await _bind_explicit_http_agent({}) is None

    @pytest.mark.asyncio
    async def test_non_string_agent_id_still_returns_none(self):
        assert await _bind_explicit_http_agent({"agent_id": 12345}) is None


class TestCorroborationRequiresCallerProof:
    """A present ``client_session_id`` is not proof by itself.

    `_inject_http_client_session` synthesizes one into every REST call that
    doesn't already carry one, so presence is satisfied by ALL traffic
    regardless of what the caller sent. `get_csid_transport_injected()` is
    the server's own record of which case a request is; corroboration must
    key on that, not on presence.
    """

    @pytest.mark.asyncio
    async def test_transport_injected_csid_does_not_corroborate(self):
        from src.mcp_handlers.context import set_csid_transport_injected

        set_csid_transport_injected(True)  # transport synthesized it
        try:
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "http:1.2.3.4:ua-fingerprint"}
                )
                == "none"
            )
        finally:
            set_csid_transport_injected(False)

    @pytest.mark.asyncio
    async def test_caller_sent_csid_still_corroborates(self):
        with _not_injected(), _resolved_session(VALID):
            assert (
                await _explicit_bind_corroboration({"agent_id": VALID, "client_session_id": "c"})
                == "csid"
            )

    @pytest.mark.asyncio
    async def test_transport_injected_csid_falls_through_at_the_gate(self):
        # The end-to-end shape of the bug the review caught: a REST call
        # that supplies ONLY agent_id, where the transport then fills in
        # client_session_id as it does for every such call, must still be
        # refused — not silently corroborated by the server's own filler.
        from src.mcp_handlers.context import set_csid_transport_injected

        set_csid_transport_injected(True)
        try:
            assert (
                await _bind_explicit_http_agent(
                    {"agent_id": VALID, "client_session_id": "http:1.2.3.4:ua-fingerprint"}
                )
                is None
            )
        finally:
            set_csid_transport_injected(False)


class TestCsidCorroborationCannotBeForgedFromTheUuidAlone:
    """Three gaps a third codex review caught in the ownership check itself."""

    @pytest.mark.asyncio
    async def test_canonical_prefix_form_does_not_corroborate_even_if_it_resolves(self):
        # The forgery: compute the deterministic session id from the public
        # uuid alone, submit it, needing no session of your own at all. Even
        # mocking resolve_session_identity to "succeed" must not matter —
        # the check must reject the canonical form BEFORE resolving it.
        canonical = make_client_session_id(VALID)
        with _not_injected(), _resolved_session(VALID) as mocked:
            result = await _explicit_bind_corroboration(
                {"agent_id": VALID, "client_session_id": canonical}
            )
        assert result == "none"
        mocked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_padded_canonical_form_still_excluded(self):
        # A fourth codex review caught this exact dodge: the exclusion
        # compared the RAW value while resolution ran on the NORMALIZED
        # (whitespace-stripped) one, so a padded canonical id slipped past
        # the check and then normalized straight into the forgeable form.
        canonical = make_client_session_id(VALID)
        with _not_injected(), _resolved_session(VALID) as mocked:
            result = await _explicit_bind_corroboration(
                {"agent_id": VALID, "client_session_id": f"  {canonical}  "}
            )
        assert result == "none"
        mocked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canonical_prefix_form_for_a_different_agent_is_unaffected(self):
        # Sanity: the exclusion is scoped to THIS agent_id's own canonical
        # form, not to every "agent-..." string — a non-canonical session id
        # that merely starts with "agent-" for some other agent must still
        # go through the normal resolve+match path.
        with _not_injected(), _resolved_session(VALID):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": make_client_session_id(OTHER)}
                )
                == "csid"
            )

    @pytest.mark.asyncio
    async def test_refused_resolution_does_not_corroborate(self):
        # _substrate_http_reject's shape: resume_failed=True carrying the
        # REJECTED uuid. Equality alone would misread this as a match.
        with _not_injected(), _resolved_session(VALID, resume_failed=True):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "none"
            )

    @pytest.mark.asyncio
    async def test_error_carrying_resolution_does_not_corroborate(self):
        with _not_injected(), _resolved_session(VALID, error="some_error"):
            assert (
                await _explicit_bind_corroboration(
                    {"agent_id": VALID, "client_session_id": "c"}
                )
                == "none"
            )

    @pytest.mark.asyncio
    async def test_stalled_lookup_degrades_to_none_instead_of_hanging(self):
        import asyncio

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        with _not_injected(), _resolved_session(None, side_effect=_hang):
            from src.http_routes import access

            original_timeout = access._CORROBORATION_LOOKUP_TIMEOUT
            access._CORROBORATION_LOOKUP_TIMEOUT = 0.05
            try:
                result = await asyncio.wait_for(
                    _explicit_bind_corroboration(
                        {"agent_id": VALID, "client_session_id": "c"}
                    ),
                    timeout=2.0,
                )
            finally:
                access._CORROBORATION_LOOKUP_TIMEOUT = original_timeout
        assert result == "none"


class TestCanaryLogHygiene:
    @pytest.mark.asyncio
    async def test_log_line_never_contains_the_uuid(self, caplog):
        # A prefix is still an identity fragment. This line is meant to be
        # safe to leave on in a live server and safe to read in a shared log.
        with caplog.at_level("INFO"):
            await _bind_explicit_http_agent({"agent_id": VALID})

        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert "[ATTEST]" in emitted
        assert VALID not in emitted
        assert VALID[:8] not in emitted

    @pytest.mark.asyncio
    async def test_log_line_reports_the_corroboration_class(self, caplog):
        with caplog.at_level("INFO"), _not_injected(), _resolved_session(VALID):
            await _bind_explicit_http_agent({"agent_id": VALID, "client_session_id": "c"})

        emitted = " ".join(r.getMessage() for r in caplog.records)
        assert "corroboration=csid" in emitted
