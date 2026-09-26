"""The strict refusal for a required call that resolved no identity says why,
and the same resolver result gets the same recovery on both transports.

Under STRICT_IDENTITY_REQUIRED a required call that resolves no identity is
refused. Its recovery depends on why nothing resolved, and both transports
pick it from the resolver's own result through
``identity_bootstrap.unbound_call_refusal``:

- A session miss used to carry the default refusal, which leads with
  ``onboard(force_new=true)``. The most common caller in that state is a
  process that called start_session and did not send its id on this call,
  and minting again splits its work across two identities. #2475 gave the
  unbound read and the strict write refusal the client_session_id-first
  order; this refusal uses the same wording source and the same order. It
  keys only on what the caller itself sent: with no client_session_id of its
  own, the retry with that id leads; with a caller-sent id that names
  nothing, repeating it cannot help, so the rebind leads.
- A session that names an identity but was refused (the hijack guard, a
  substrate-anchored resident over HTTP) is not a miss, and telling the
  caller its id "names no identity" is false. /mcp/ always said which guard
  refused; REST, whose prebind returns only "no binding", now records the
  resolver's result and says the same.
- A session lookup that raised (``pg_lookup_exception``) is a server-side
  failure: the id may be fine, so the retry leads on both transports.

Every case runs the real resolver (``resolve_session_identity``), the real
session-key derivation, and on REST the real session injection and prebind,
so each refusal is built from what the producer actually returned. Only
storage (the Redis session cache, the session table, the substrate claim,
the onboard pin, the sticky cache, the operator lookup) is stubbed.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from src.mcp_handlers.context import SessionSignals
from src.mcp_handlers.identity_bootstrap import (
    CALLER_PROOF_REMEDY,
    DO_NOT_MINT_A_SECOND_IDENTITY,
    SESSION_ID_NAMES_NO_IDENTITY,
    identity_refusal_status,
)
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")

STALE_SESSION = "agent-0badc0de-000"
HEADER = "agent-1856bb5c-255"
RESIDENT = "5a0e3c1d-77b2-4c1d-9e0f-1a2b3c4d5e6f"
USER_AGENT = "curl/8.7.1"
MCP_FINGERPRINT = "127.0.0.1:abc123"
# A truncated token: extract_token_agent_uuid_safe returns None, the shape of
# the #1319 incident.
TRUNCATED_TOKEN = "v1.eyJhaWQiOiJ0cnVuY2F0ZWQifQ.dGr"


def _stub_storage(
    stack: ExitStack,
    *,
    redis_binding: dict | None = None,
    pg_raises: bool = False,
    substrate_resident: bool = False,
) -> None:
    """Stub what the real resolver and both transports read."""
    from src.mcp_handlers.identity import resolution as res
    from src.substrate.verification import SubstrateClaim

    fake_redis = None
    if redis_binding is not None:
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=dict(redis_binding))
    db = AsyncMock()
    if pg_raises:
        db.get_session = AsyncMock(side_effect=RuntimeError("session pool closed"))
    else:
        db.get_session = AsyncMock(return_value=None)
    db.update_session_activity = AsyncMock(return_value=True)
    claim = None
    if substrate_resident:
        claim = SubstrateClaim(
            agent_id=RESIDENT,
            expected_launchd_label="com.example.resident",
            expected_executable_path="/usr/local/bin/resident",
            enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            enrolled_by_operator=True,
        )
    broadcaster = MagicMock()
    broadcaster.broadcast_event = AsyncMock()

    stack.enter_context(patch.object(res, "_get_redis", return_value=fake_redis))
    stack.enter_context(patch.object(res, "get_db", return_value=db))
    stack.enter_context(patch("src.db.get_db", MagicMock(return_value=db)))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.handlers._broadcaster", return_value=broadcaster,
    ))
    stack.enter_context(patch(
        "src.substrate.verification.fetch_substrate_claim", AsyncMock(return_value=claim),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.session.lookup_onboard_pin", AsyncMock(return_value=None),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.middleware.identity_step._load_binding_from_redis",
        AsyncMock(return_value=None),
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.middleware.identity_step._transport_identity_cache", {},
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        AsyncMock(return_value=None),
    ))


async def _mcp_refusal(
    monkeypatch, tool_name: str, arguments: dict, *, storage=None, **signal_fields
):
    """The real identity step, derivation and resolver on /mcp/."""
    from src.mcp_handlers.context import reset_session_signals, set_session_signals
    from src.mcp_handlers.middleware import DispatchContext, resolve_identity

    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    fields = {
        "ip_ua_fingerprint": MCP_FINGERPRINT,
        "user_agent": USER_AGENT,
        "transport": "mcp",
    }
    fields.update(signal_fields)
    token = set_session_signals(SessionSignals(**fields))
    try:
        with ExitStack() as stack:
            _stub_storage(stack, **(storage or {}))
            result = await resolve_identity(tool_name, dict(arguments), DispatchContext())
    finally:
        reset_session_signals(token)
    assert isinstance(result, list), "expected the strict refusal"
    payload = json.loads(result[0].text)
    assert identity_refusal_status(payload) == "identity_required"
    return payload


def _request(headers: dict) -> Request:
    raw = [(b"user-agent", USER_AGENT.encode())]
    raw += [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/tools/call",
        "headers": raw,
        "client": ("127.0.0.1", 43210),
    })


async def _rest_refusal(
    monkeypatch, tool_name: str, headers: dict, body: dict, *, storage=None
):
    """The REST route's own order (http_routes/tools.http_call_tool and
    _execute_http_tool_in_context): inject the session, open an unbound
    request context, prebind, then execute_http_tool's strict gate."""
    from src.http_routes import access
    from src.http_routes.tools import _inject_http_client_session
    from src.mcp_handlers.context import (
        reset_session_context,
        reset_session_signals,
        set_session_context,
        set_session_signals,
    )
    from src.services.http_tool_service import _strict_identity_refusal_or_none

    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    request = _request(headers)
    arguments = dict(body)
    with ExitStack() as stack:
        _stub_storage(stack, **(storage or {}))
        session_id = await _inject_http_client_session(request, arguments)
        signals = access._build_http_session_signals(request)
        signals_token = set_session_signals(signals)
        context_token = set_session_context(
            session_key=session_id, client_session_id=session_id,
        )
        try:
            bound = await access._resolve_http_bound_agent(tool_name, arguments, signals)
            payload = _strict_identity_refusal_or_none(tool_name, arguments)
        finally:
            reset_session_context(context_token)
            reset_session_signals(signals_token)
    assert bound is None
    assert payload is not None, "expected the strict refusal"
    assert identity_refusal_status(payload) == "identity_required"
    return payload


def _recovery(payload: dict) -> dict:
    return {k: payload[k] for k in ("hint", "next_step", "safe_options", "do_not")}


def _mint_calls(payload: dict) -> list[str]:
    return [o["call"] for o in payload["safe_options"] if "force_new" in o["call"]]


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


# ─── Session miss ──────────────────────────────────────────────────────────


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
    "rest_headers, rest_body, mcp_signals, mcp_arguments, caller_sent",
    [
        # The REST transport puts a fingerprint-derived id on the call.
        pytest.param({}, {}, {}, {}, False, id="fingerprint-injected"),
        # The REST transport derives the id from an X-Session-ID header:
        # caller asserted, but not a client_session_id the caller sent.
        pytest.param(
            {"X-Session-ID": HEADER}, {}, {"x_session_id": HEADER}, {}, False,
            id="x-session-id-header",
        ),
        # The caller sent the id in the body.
        pytest.param(
            {}, {"client_session_id": STALE_SESSION},
            {}, {"client_session_id": STALE_SESSION}, True,
            id="body-id",
        ),
        # An id that normalizes to nothing (symbols or whitespace only) is
        # dropped by each transport's derivation and never looked up, so
        # "names no identity" would be false. It gets the no-id recovery
        # (pass the id start_session returned), whose wording does not claim
        # that nothing was sent; the same as an omitted or empty id.
        pytest.param(
            {}, {"client_session_id": "!!!"},
            {}, {"client_session_id": "!!!"}, False,
            id="invalid-body-id",
        ),
        pytest.param(
            {}, {"client_session_id": "   "},
            {}, {"client_session_id": "   "}, False,
            id="whitespace-body-id",
        ),
    ],
)
async def test_rest_and_mcp_session_miss_refusals_match_for_the_same_caller_sent_fact(
    monkeypatch, rest_headers, rest_body, mcp_signals, mcp_arguments, caller_sent
):
    """Same caller-sent fact, same recovery, both transports."""
    rest = await _rest_refusal(monkeypatch, "sync_state", rest_headers, rest_body)
    mcp = await _mcp_refusal(monkeypatch, "sync_state", mcp_arguments, **mcp_signals)

    assert _recovery(rest) == _recovery(mcp)
    assert rest["surface_context"]["transport_surface"] == "rest_tool_call"
    if caller_sent:
        assert rest["hint"].startswith(SESSION_ID_NAMES_NO_IDENTITY)
    else:
        _assert_leads_with_the_callers_own_id(rest, "sync_state")


# ─── A session that names an identity but was refused ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token", [pytest.param(None, id="no-token"), pytest.param(TRUNCATED_TOKEN, id="bad-token")]
)
async def test_a_hijack_guard_rejection_says_so_on_both_transports(monkeypatch, token):
    """The session names an identity bound under another fingerprint, and the
    strict fingerprint guard refuses the resume (#1319). The id is not
    unknown, so the session-miss recovery would be false here."""
    monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "strict")
    body = {"client_session_id": STALE_SESSION}
    if token:
        body["continuity_token"] = token
    storage = {
        "redis_binding": {"agent_id": RESIDENT, "bind_ip_ua": "10.9.9.9:elsewhere"},
    }

    rest = await _rest_refusal(monkeypatch, "sync_state", {}, body, storage=storage)
    mcp = await _mcp_refusal(monkeypatch, "sync_state", body, storage=storage)

    assert _recovery(rest) == _recovery(mcp)
    assert "hijack guard (fingerprint_mismatch)" in rest["hint"]
    assert SESSION_ID_NAMES_NO_IDENTITY not in rest["hint"]
    for payload in (rest, mcp):
        surface = payload["surface_context"]
        assert surface["resume_rejected_reason"] == "fingerprint_mismatch"
        assert surface.get("continuity_token_invalid") is (True if token else None)
    assert ("failed verification" in rest["hint"]) is bool(token)


@pytest.mark.asyncio
async def test_a_substrate_resident_over_http_is_sent_to_its_socket_not_to_a_mint(
    monkeypatch,
):
    """A substrate-anchored resident's session resumed over TCP. The resolver
    refuses it and says why; a mint would give the resident a second
    identity."""
    body = {"client_session_id": f"agent-{RESIDENT[:12]}"}
    storage = {"redis_binding": {"agent_id": RESIDENT}, "substrate_resident": True}

    rest = await _rest_refusal(monkeypatch, "sync_state", {}, body, storage=storage)
    mcp = await _mcp_refusal(monkeypatch, "sync_state", body, storage=storage)

    assert _recovery(rest) == _recovery(mcp)
    assert "UNITARES_UDS_SOCKET" in rest["hint"]
    assert rest["safe_options"][0]["action"] == "use_attested_uds"
    assert _mint_calls(rest) == []
    assert SESSION_ID_NAMES_NO_IDENTITY not in rest["hint"]
    for payload in (rest, mcp):
        assert (
            payload["surface_context"]["resume_rejected_reason"]
            == "substrate_anchored_uuid_requires_uds"
        )


# ─── A server-side failure ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"client_session_id": STALE_SESSION}, id="caller-sent-id"),
        pytest.param({}, id="no-caller-id"),
    ],
)
async def test_a_session_lookup_that_raised_is_retried_not_minted(monkeypatch, body):
    """session_resolve_miss with reason pg_lookup_exception: the session
    table could not be read, so the id may be fine and a retry may succeed."""
    storage = {"pg_raises": True}

    rest = await _rest_refusal(monkeypatch, "sync_state", {}, body, storage=storage)
    mcp = await _mcp_refusal(monkeypatch, "sync_state", body, storage=storage)

    assert _recovery(rest) == _recovery(mcp)
    assert rest["safe_options"][0]["action"] == "retry"
    assert _mint_calls(rest) == []
    assert SESSION_ID_NAMES_NO_IDENTITY not in rest["hint"]
    for payload in (rest, mcp):
        surface = payload["surface_context"]
        assert surface["identity_resolution"] == "failed"
        assert surface["identity_resolution_failure"] == "pg_lookup_exception"


def test_a_result_with_no_error_and_no_binding_is_a_server_failure():
    """No producer returns this on purpose, which is the point: a resolver
    result that neither binds nor names an error is a server-side fault, as
    the /mcp/ middleware's unusable_result branch already treats it."""
    from src.mcp_handlers.identity_bootstrap import unbound_call_refusal

    options, surface = unbound_call_refusal(
        "sync_state", {}, caller_sent_session_id=True,
    )

    assert options["safe_options"][0]["action"] == "retry"
    assert surface["identity_resolution_failure"] == "unusable_result"


def test_the_bare_onboard_refusal_still_leads_with_the_mint():
    """The defaults are right for the lineage refusal (onboard is the
    identity-establishing call) and stay as they were."""
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload

    payload = strict_identity_refusal_payload(
        "onboard", status="lineage_declaration_required", hint="ambiguous"
    )
    assert payload["safe_options"][0]["call"] == "onboard(force_new=true)"
    assert payload["next_step"].startswith("Call onboard(force_new=true)")
