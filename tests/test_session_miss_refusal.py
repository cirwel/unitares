"""The strict refusal for a call that resolved no identity leads with the
caller's own client_session_id, not with a second mint.

Under STRICT_IDENTITY_REQUIRED a required call whose session resolves nothing
(``session_resolve_miss`` on /mcp/, an unbound required call on REST) is
refused. It used to carry the default refusal, which leads with
``onboard(force_new=true)``. The most common caller in that state is a process
that called start_session and did not send its id on this call, and minting
again splits its work across two identities. #2475 gave the unbound read and
the strict write refusal the client_session_id-first order; this refusal now
uses the same wording source (identity_bootstrap) and the same order.

The branch keys only on what the caller itself sent. With no client_session_id
of its own, the retry with that id leads. With a caller-sent id that names
nothing, repeating it cannot help, so the rebind leads and the mint is for a
process with nothing to rebind to.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.context import SessionSignals
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")
from src.mcp_handlers.identity_bootstrap import (
    CALLER_PROOF_REMEDY,
    DO_NOT_MINT_A_SECOND_IDENTITY,
    SESSION_ID_NAMES_NO_IDENTITY,
    identity_refusal_status,
)

STALE_SESSION = "agent-0badc0de-000"
HEADER = "agent-1856bb5c-255"


def _miss(session_key: str = "fp-session"):
    return {"resume_failed": True, "error": "session_resolve_miss", "session_key": session_key}


async def _mcp_refusal(monkeypatch, tool_name: str, arguments: dict, **signal_fields):
    """The real identity step and derivation; only storage is stubbed."""
    from src.mcp_handlers.context import reset_session_signals, set_session_signals
    from src.mcp_handlers.middleware import DispatchContext, resolve_identity
    from src.mcp_handlers.middleware import identity_step

    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    fields = {
        "ip_ua_fingerprint": "127.0.0.1:abc123",
        "user_agent": "curl/8.7.1",
        "transport": "mcp",
    }
    fields.update(signal_fields)
    db = MagicMock()
    db.update_session_activity = AsyncMock(return_value=True)
    resolve = AsyncMock(return_value=_miss())
    token = set_session_signals(SessionSignals(**fields))
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(identity_step, "_transport_identity_cache", {}))
            stack.enter_context(patch(
                "src.cache.redis_client.get_redis", AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
                AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.session.lookup_onboard_pin",
                AsyncMock(return_value=None),
            ))
            stack.enter_context(patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve,
            ))
            stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
            result = await resolve_identity(tool_name, dict(arguments), DispatchContext())
    finally:
        reset_session_signals(token)
    assert isinstance(result, list), "expected the strict refusal"
    payload = json.loads(result[0].text)
    assert identity_refusal_status(payload) == "identity_required"
    return payload


def _rest_refusal(monkeypatch, tool_name: str, arguments: dict, *, injected: bool, source):
    from src.mcp_handlers.context import (
        reset_csid_injected_source,
        reset_csid_transport_injected,
        set_csid_injected_source,
        set_csid_transport_injected,
    )
    from src.services.http_tool_service import _strict_identity_refusal_or_none

    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    flag = set_csid_transport_injected(injected)
    src_token = set_csid_injected_source(source)
    try:
        with patch(
            "src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None
        ):
            payload = _strict_identity_refusal_or_none(tool_name, dict(arguments))
    finally:
        reset_csid_injected_source(src_token)
        reset_csid_transport_injected(flag)
    assert payload is not None
    return payload


def _recovery(payload: dict) -> dict:
    return {k: payload[k] for k in ("hint", "next_step", "safe_options", "do_not")}


def _assert_leads_with_the_callers_own_id(payload: dict, tool_name: str) -> None:
    hint = payload["hint"]
    assert CALLER_PROOF_REMEDY in hint
    assert "onboard(" not in hint and "force_new" not in hint

    next_step = payload["next_step"]
    assert next_step.index("client_session_id") < next_step.index("continuity_token")
    assert next_step.index("continuity_token") < next_step.index("never called start_session")
    assert next_step.index("never called start_session") < next_step.index("force_new=true")

    actions = [option["action"] for option in payload["safe_options"]]
    assert actions[0] == "retry_with_client_session_id"
    assert actions.index("rebind_then_retry") < actions.index("start_session_first")
    assert payload["safe_options"][0]["call"].startswith(f"{tool_name}(")
    assert "client_session_id=<from start_session>" in payload["safe_options"][0]["call"]
    # Only the mint options mint, and each is for a process with no identity.
    for option in payload["safe_options"]:
        if "force_new" in option["call"]:
            assert option["action"] in {"start_session_first", "declare_lineage"}, option
            assert "never called start_session" in option["when"], option
    assert DO_NOT_MINT_A_SECOND_IDENTITY in payload["do_not"]
    assert any("bare identity" in line for line in payload["do_not"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signals",
    [
        pytest.param({}, id="fingerprint-only"),
        # A header naming no session is not a client_session_id the caller
        # sent on the call; the retry with its own id is still the advice.
        pytest.param({"x_session_id": HEADER}, id="x-session-id-names-nothing"),
        pytest.param({"mcp_session_id": "conn-1"}, id="mcp-session-id"),
    ],
)
@pytest.mark.parametrize("tool_name", ["sync_state", "store_finding"])
async def test_mcp_session_miss_without_a_caller_id_leads_with_that_id(
    monkeypatch, tool_name, signals
):
    payload = await _mcp_refusal(monkeypatch, tool_name, {}, **signals)

    _assert_leads_with_the_callers_own_id(payload, tool_name)
    assert payload["surface_context"]["transport_surface"] == "mcp_dispatch"


@pytest.mark.asyncio
async def test_mcp_session_miss_with_a_caller_id_that_names_nothing_leads_with_the_rebind(
    monkeypatch,
):
    payload = await _mcp_refusal(
        monkeypatch, "sync_state", {"client_session_id": STALE_SESSION}
    )

    assert payload["hint"].startswith(SESSION_ID_NAMES_NO_IDENTITY)
    actions = [option["action"] for option in payload["safe_options"]]
    # Repeating the id cannot help, so no option retries with it.
    assert "retry_with_client_session_id" not in actions
    assert actions[0] == "rebind_then_retry"
    mint = next(o for o in payload["safe_options"] if o["action"] == "start_session_first")
    assert "cannot rebind" in mint["when"]
    next_step = payload["next_step"]
    assert next_step.index("identity(") < next_step.index("force_new=true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, injected, source, caller_sent",
    [
        # The transport put a fingerprint-derived id on the call.
        pytest.param(
            {"client_session_id": "http:127.0.0.1:9aaaaac6dead"}, True, "ip_ua_fingerprint",
            False, id="fingerprint-injected",
        ),
        # The transport derived the id from an X-Session-ID header: caller
        # asserted, but not a client_session_id the caller sent.
        pytest.param(
            {"client_session_id": HEADER}, False, "x_session_id", False,
            id="x-session-id-header",
        ),
        # The caller sent the id in the body.
        pytest.param(
            {"client_session_id": STALE_SESSION}, False, None, True, id="body-id",
        ),
    ],
)
async def test_rest_and_mcp_refusals_match_for_the_same_caller_sent_fact(
    monkeypatch, arguments, injected, source, caller_sent
):
    """Same caller-sent fact, same recovery, both transports."""
    rest = _rest_refusal(
        monkeypatch, "sync_state", arguments, injected=injected, source=source
    )
    mcp = await _mcp_refusal(
        monkeypatch,
        "sync_state",
        {"client_session_id": STALE_SESSION} if caller_sent else {},
    )

    assert _recovery(rest) == _recovery(mcp)
    assert rest["surface_context"]["transport_surface"] == "rest_tool_call"
    if caller_sent:
        assert rest["hint"].startswith(SESSION_ID_NAMES_NO_IDENTITY)
    else:
        _assert_leads_with_the_callers_own_id(rest, "sync_state")


def test_the_bare_onboard_refusal_still_leads_with_the_mint():
    """The defaults are right for the lineage refusal (onboard is the
    identity-establishing call) and stay as they were."""
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload

    payload = strict_identity_refusal_payload(
        "onboard", status="lineage_declaration_required", hint="ambiguous"
    )
    assert payload["safe_options"][0]["call"] == "onboard(force_new=true)"
    assert payload["next_step"].startswith("Call onboard(force_new=true)")
