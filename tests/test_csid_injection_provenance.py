"""A caller proven via header is caller-asserted; a fingerprint pin hit is not.

`_inject_http_client_session` used to flag `client_session_id` as
transport-injected whenever the REQUEST BODY lacked the key — regardless of
whether the value it then filled in was a genuinely fabricated fallback (no
signal at all) or one `derive_session_key` resolved from a real request
signal, such as the documented `X-Session-ID` header (docs reference:
`src/http_routes/health.py`'s "Use X-Session-ID or the client_session_id
returned by onboard").

A dialectic adversarial review of the explicit-bind corroboration gate (this
PR) caught the first-order consequence: a caller who authenticates via the
X-Session-ID header and separately names an explicit `agent_id` would be
misclassified as offering no corroboration at all and wrongly refused —
inverted relative to actual proof strength, since that same caller sending a
meaningless `continuity_token` string would (pre-fix) have passed.

The first fix approximated "caller-asserted" locally (`result == ip_ua_fp and
not x_session_id`) and a codex review caught that it was still wrong for a
real branch: an onboard-PIN hit (`_mark("pinned_onboard_session")`, step 7 of
`_derive_session_key_impl`'s priority ladder) resolves to a stable,
non-fingerprint value with no `x_session_id` present — so the local
approximation read it as caller-asserted. But `_mark()` itself classifies a
pin hit as `server_inferred` (`"pinned_onboard_session"` is NOT in
`_CALLER_ASSERTED_SOURCES`), because the pin key is IP+UA fingerprint, which
`_extract_base_fingerprint`'s own docstring notes can be shared by unrelated
callers behind the same proxy pool or client string. `_extract_client_session_id`
now reads `get_session_proof_origin()` — the single source `_mark()` already
writes — instead of re-deriving the distinction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from src.http_api import _extract_client_session_id
from src.http_routes.tools import _inject_http_client_session
from src.mcp_handlers.context import get_csid_transport_injected, set_session_proof_origin


def _request(*, headers=()) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": list(headers),
        "client": ("127.0.0.1", 43210),
    }
    return Request(scope)


def _patched_resolution(return_value: str, proof_origin: str):
    """Stand in for `derive_session_key` + the `_mark()` call it makes.

    Both are patched together because `_extract_client_session_id` reads
    `get_session_proof_origin()` immediately after awaiting
    `derive_session_key` — mocking the return value alone (as a first draft
    of this test file did) leaves the proof-origin contextvar at whatever a
    prior test left it, which is exactly the kind of test-passes-for-the-
    wrong-reason gap a codex review is for.
    """

    async def _resolve(*_args, **_kwargs):
        set_session_proof_origin(proof_origin)
        return return_value

    return patch(
        "src.mcp_handlers.identity.handlers.derive_session_key",
        new_callable=AsyncMock,
        side_effect=_resolve,
    )


class TestExtractClientSessionIdProvenance:
    @pytest.mark.asyncio
    async def test_no_signal_at_all_is_not_caller_asserted(self):
        # derive_session_key found nothing better than the raw fingerprint,
        # and there's no X-Session-ID header — the genuine fallback case.
        # `_mark("ip_ua_fingerprint")` would set server_inferred here too;
        # this test forces the same conclusion via the ip_ua_fp == result
        # branch, which independently overrides to False regardless.
        req = _request()
        from src.http_api import _build_http_session_signals

        signals = _build_http_session_signals(req)
        with _patched_resolution(signals.ip_ua_fingerprint, "server_inferred"):
            csid, is_caller_asserted = await _extract_client_session_id(req)

        assert is_caller_asserted is False
        assert csid  # still a usable, if fabricated, id

    @pytest.mark.asyncio
    async def test_x_session_id_header_is_caller_asserted(self):
        # A real X-Session-ID header is present; derive_session_key resolves
        # off it and _mark("x_session_id") — in _CALLER_ASSERTED_SOURCES —
        # would fire. Must count as proof even though it arrived via header
        # rather than body.
        req = _request(headers=[(b"x-session-id", b"agent-abc123-real-session")])
        with _patched_resolution("agent-abc123-real-session", "caller_asserted"):
            csid, is_caller_asserted = await _extract_client_session_id(req)

        assert is_caller_asserted is True
        assert csid == "agent-abc123-real-session"

    @pytest.mark.asyncio
    async def test_resolved_pin_is_not_caller_asserted(self):
        # The case a codex review caught: a stable, non-fingerprint value
        # with no X-Session-ID header LOOKS caller-provided but is a network-
        # fingerprint pin hit — _mark("pinned_onboard_session") classifies it
        # server_inferred, and this must agree, or an attacker sharing a pin
        # key (proxy pool / UA string) with someone else's recent onboard
        # could corroborate an unrelated agent_id claim.
        req = _request()
        with _patched_resolution("pinned-session-key-xyz", "server_inferred"):
            csid, is_caller_asserted = await _extract_client_session_id(req)

        assert is_caller_asserted is False
        assert csid == "pinned-session-key-xyz"


class TestInjectHttpClientSessionFlagsCorrectly:
    @pytest.mark.asyncio
    async def test_body_supplied_csid_is_never_injected(self):
        req = _request()
        arguments = {"client_session_id": "caller-sent-this"}
        await _inject_http_client_session(req, arguments)
        assert get_csid_transport_injected() is False

    @pytest.mark.asyncio
    async def test_header_derived_csid_is_not_flagged_injected(self):
        req = _request(headers=[(b"x-session-id", b"agent-abc123-real-session")])
        arguments: dict = {}
        with _patched_resolution("agent-abc123-real-session", "caller_asserted"):
            await _inject_http_client_session(req, arguments)

        assert get_csid_transport_injected() is False
        assert arguments["client_session_id"] == "agent-abc123-real-session"

    @pytest.mark.asyncio
    async def test_pin_hit_csid_is_flagged_injected(self):
        req = _request()
        arguments: dict = {}
        with _patched_resolution("pinned-session-key-xyz", "server_inferred"):
            await _inject_http_client_session(req, arguments)

        assert get_csid_transport_injected() is True

    @pytest.mark.asyncio
    async def test_no_signal_fallback_is_flagged_injected(self):
        req = _request()
        arguments: dict = {}
        from src.http_api import _build_http_session_signals

        signals = _build_http_session_signals(req)
        with _patched_resolution(signals.ip_ua_fingerprint, "server_inferred"):
            await _inject_http_client_session(req, arguments)

        assert get_csid_transport_injected() is True
