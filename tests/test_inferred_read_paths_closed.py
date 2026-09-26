"""A pre-onboard self-read never shows state from a binding the server only
inferred.

The principle is the #839 guard in core.handle_get_governance_metrics, and
the #945 short-circuit in the /mcp/ identity step is its rule: a pre_onboard
read resolves an identity only on proof the caller sent in this request (a
client_session_id, a verified continuity_token, or an X-Session-ID header).
Three paths still served a server-inferred binding's real state
(identified in #2478's probes):

- P1, /mcp/: the sticky transport binding was consulted before the
  short-circuit. Its key is the IP:UA fingerprint plus a client-sent
  Mcp-Session-Id (Claude Code subagents share their parent's connection), or
  the bare fingerprint over UDS, so a proof-less read got the state of
  whichever agent last resolved on the connection. Direct and through
  use_tool.
- P2, /mcp/: a UUID X-Agent-Id header, and an agent_uuid argument (which
  reaches a metrics read only through use_tool), counted as read proof. The
  session-key derivation ignores both, so the read resolved on the
  transport's own signals (the onboard pin here) and served that binding.
- P3, REST: a body whose client_session_id is null or empty, with no session
  header, is the one REST read the transport leaves cacheable, and the sticky
  consult ran ahead of the prebind's read gate. Direct and through use_tool.

Every case runs through the real transport path (tests/helpers/
read_transport.py). Writes keep the sticky binding: scope checks pin that
the closure applies to reads only.

The last group pins what the corrected descriptions of check_working_state
and get_governance_metrics say about an explicit agent_id: /mcp/ drops it,
and through use_tool or REST it names the agent to read, marked
identity_assurance.caller_proven=false when the call's session was inferred.
"""

from __future__ import annotations

import pytest

from tests.helpers import read_transport as rt
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")

ROUTES = ["direct", "use_tool"]
READS = ["check_working_state", "get_governance_metrics"]


def _assert_unbound(seen):
    assert rt.is_unbound(seen["result"]), seen["result"]
    assert seen["metrics_served"] is False
    assert seen["resolve_calls"] == []


# ─── P1: the sticky binding on /mcp/ ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("read", READS)
async def test_p1_a_connections_sticky_binding_does_not_answer_a_proofless_read(
    monkeypatch, route, read
):
    """The sibling-read case: a Claude Code subagent on its parent's
    connection sends the same Mcp-Session-Id, and the parent's earlier call
    left a sticky binding under that key."""
    signals = rt.signals(mcp_session_id=rt.CONNECTION)
    sticky = {f"sticky:{rt.FINGERPRINT}:{rt.CONNECTION}": rt.sticky_binding()}

    seen = await rt.mcp_call(monkeypatch, route, read, signals, sticky=sticky)

    _assert_unbound(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_p1_the_fingerprint_sticky_binding_over_uds_does_not_answer_a_proofless_read(
    monkeypatch, route
):
    """Over UDS the sticky key is the bare fingerprint, shared by every
    process on the host with the same user agent."""
    signals = rt.signals(transport="uds", peer_pid=4242)
    sticky = {f"sticky:{rt.FINGERPRINT}": rt.sticky_binding()}

    seen = await rt.mcp_call(
        monkeypatch, route, "check_working_state", signals, sticky=sticky
    )

    _assert_unbound(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_p1_a_read_that_carries_proof_still_resolves_on_it(monkeypatch, route):
    """The closure removes the inference, not the read: the caller's own
    client_session_id still reads its own state on the same connection."""
    signals = rt.signals(mcp_session_id=rt.CONNECTION)
    sticky = {f"sticky:{rt.FINGERPRINT}:{rt.CONNECTION}": rt.sticky_binding()}

    seen = await rt.mcp_call(
        monkeypatch,
        route,
        "check_working_state",
        signals,
        target_arguments={"client_session_id": rt.AGENT_SESSION},
        sticky=sticky,
    )

    assert seen["resolve_calls"] == [rt.AGENT_SESSION]
    assert seen["metrics_for"] == [rt.AGENT_UUID]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_p1_a_write_still_uses_the_sticky_binding(monkeypatch, route):
    """Scope check: only reads skip the sticky binding. A write keeps it, and
    stays server-inferred, which the strict write gate refuses unless the
    agent is substrate-earned."""
    signals = rt.signals(mcp_session_id=rt.CONNECTION)
    sticky = {f"sticky:{rt.FINGERPRINT}:{rt.CONNECTION}": rt.sticky_binding()}

    seen = await rt.mcp_call(monkeypatch, route, "sync_state", signals, sticky=sticky)

    assert seen["bound"] == rt.AGENT_UUID
    assert seen["source"] == "sticky_cache:explicit_client_session_id"
    assert seen["proof_origin"] == "server_inferred"


# ─── P2: X-Agent-Id and agent_uuid on /mcp/ ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("read", READS)
async def test_p2_a_uuid_x_agent_id_header_is_not_read_proof(monkeypatch, route, read):
    """The header names another agent, and the read used to resolve on the
    onboard pin instead and serve the pinned agent's state."""
    signals = rt.signals(x_agent_id=rt.OTHER_UUID)

    seen = await rt.mcp_call(monkeypatch, route, read, signals, pinned=rt.AGENT_SESSION)

    _assert_unbound(seen)


@pytest.mark.asyncio
async def test_p2_an_agent_uuid_argument_is_not_read_proof(monkeypatch):
    """agent_uuid reaches a metrics read only through use_tool; the /mcp/
    schema of the metrics tools drops it on a direct call."""
    seen = await rt.mcp_call(
        monkeypatch,
        "use_tool",
        "check_working_state",
        rt.signals(),
        target_arguments={"agent_uuid": rt.OTHER_UUID},
        pinned=rt.AGENT_SESSION,
    )

    _assert_unbound(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_p2_x_agent_id_on_a_client_sent_connection_is_not_read_proof(
    monkeypatch, route
):
    """With an Mcp-Session-Id the read resolved on the connection-scoped
    session key, which the derivation marks caller_asserted, so not even the
    #2472 caller_proven=false mark was applied."""
    signals = rt.signals(x_agent_id=rt.OTHER_UUID, mcp_session_id=rt.CONNECTION)

    seen = await rt.mcp_call(monkeypatch, route, "check_working_state", signals)

    _assert_unbound(seen)


# ─── P3: the sticky binding on REST ──────────────────────────────────────

# A body client_session_id the REST injection leaves in place and the sticky
# consult treats as absent.
NULL_BODY_IDS = [pytest.param(None, id="null"), pytest.param("", id="empty")]


@pytest.mark.asyncio
@pytest.mark.parametrize("body_id", NULL_BODY_IDS)
@pytest.mark.parametrize("read", READS)
async def test_p3_a_rest_body_naming_no_session_does_not_read_the_sticky_binding(
    monkeypatch, body_id, read
):
    sticky = {f"sticky:{rt.REST_FINGERPRINT}": rt.sticky_binding()}

    payload, metrics, resolve = await rt.rest_call(
        monkeypatch, read, {"client_session_id": body_id}, sticky=sticky,
    )

    assert rt.is_unbound(payload), payload
    assert metrics.await_count == 0
    # The read gate judged the fingerprint derivation and never looked it up.
    assert resolve.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body_id", NULL_BODY_IDS)
async def test_p3_through_use_tool_a_rest_body_naming_no_session_stays_unbound(
    monkeypatch, body_id
):
    sticky = {f"sticky:{rt.REST_FINGERPRINT}": rt.sticky_binding()}

    payload, metrics, _resolve = await rt.rest_call(
        monkeypatch,
        "use_tool",
        {
            "tool_name": "check_working_state",
            "arguments": {"client_session_id": body_id},
        },
        sticky=sticky,
    )

    assert rt.is_unbound(payload), payload
    assert metrics.await_count == 0


@pytest.mark.asyncio
async def test_p3_a_rest_write_still_uses_the_sticky_binding():
    """Scope check, REST: a required call keeps the sticky binding."""
    from unittest.mock import AsyncMock, patch

    from src.http_routes import access
    from src.mcp_handlers.context import (
        get_context_resolved_agent_id,
        get_session_proof_origin,
        reset_session_context,
        set_session_context,
    )

    sticky = {f"sticky:{rt.REST_FINGERPRINT}": rt.sticky_binding()}
    request = rt.rest_request()
    with patch(
        "src.mcp_handlers.middleware.identity_step._transport_identity_cache", sticky,
    ), patch(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        AsyncMock(return_value=None),
    ):
        token = set_session_context(session_key=None, client_session_id=None)
        try:
            bound = await access._resolve_http_bound_agent(
                "process_agent_update",
                {"client_session_id": None},
                access._build_http_session_signals(request),
            )
            context_bound = get_context_resolved_agent_id()
            proof = get_session_proof_origin()
        finally:
            reset_session_context(token)

    assert bound == context_bound == rt.AGENT_UUID
    assert proof == "server_inferred"


# ─── What the descriptions say about an explicit agent_id ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("read", READS)
async def test_mcp_drops_agent_id_on_a_direct_read(monkeypatch, read):
    """Neither metrics tool declares agent_id on /mcp/, so FastMCP drops it
    and the call is a proof-less self-read."""
    seen = await rt.mcp_call(
        monkeypatch, "direct", read, rt.signals(),
        target_arguments={"agent_id": rt.OTHER_UUID},
    )

    _assert_unbound(seen)


@pytest.mark.asyncio
async def test_through_use_tool_agent_id_names_the_agent_and_an_inferred_call_is_marked(
    monkeypatch,
):
    seen = await rt.mcp_call(
        monkeypatch, "use_tool", "check_working_state", rt.signals(),
        target_arguments={"agent_id": rt.OTHER_UUID},
    )

    assert seen["metrics_for"] == [rt.OTHER_UUID]
    assurance = seen["result"]["identity_assurance"]
    assert assurance["caller_proven"] is False
    assert assurance["session_source"] == "ip_ua_fingerprint"


@pytest.mark.asyncio
async def test_over_rest_agent_id_names_the_agent_and_an_inferred_call_is_marked(
    monkeypatch,
):
    payload, metrics, _resolve = await rt.rest_call(
        monkeypatch, "check_working_state", {"agent_id": rt.OTHER_UUID},
    )

    assert [call.args[0] for call in metrics.await_args_list] == [rt.OTHER_UUID]
    assert payload["identity_assurance"]["caller_proven"] is False


@pytest.mark.asyncio
async def test_a_proven_self_read_carries_no_mark(monkeypatch):
    seen = await rt.mcp_call(
        monkeypatch, "direct", "check_working_state", rt.signals(),
        target_arguments={"client_session_id": rt.AGENT_SESSION},
    )

    assert seen["metrics_for"] == [rt.AGENT_UUID]
    assert "identity_assurance" not in seen["result"]


# The descriptions scope that claim: agent_id names the agent to read unless
# the caller is bound as a different agent, which inject_identity refuses
# with identity_mismatch on every route that runs it (/mcp/ use_tool for
# both reads, check_working_state over REST). REST get_governance_metrics
# runs a direct handler with no inject step and answers the named agent.


def _mismatch(payload) -> bool:
    if isinstance(payload, list):
        import json

        payload = json.loads(payload[0].text)
    details = payload.get("details") or {}
    return details.get("error_type") == "identity_mismatch" or (
        payload.get("error_type") == "identity_mismatch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("read", READS)
async def test_through_use_tool_a_caller_bound_as_another_agent_is_refused(
    monkeypatch, read
):
    seen = await rt.mcp_call(
        monkeypatch, "use_tool", read, rt.signals(),
        target_arguments={
            "client_session_id": rt.AGENT_SESSION,
            "agent_id": rt.OTHER_UUID,
        },
    )

    assert _mismatch(seen["result"]), seen["result"]
    assert seen["metrics_served"] is False


REST_CHECK_WORKING_STATE = [
    pytest.param(
        "check_working_state",
        {"client_session_id": rt.AGENT_SESSION, "agent_id": rt.OTHER_UUID},
        id="direct",
    ),
    pytest.param(
        "use_tool",
        {
            "tool_name": "check_working_state",
            "arguments": {
                "client_session_id": rt.AGENT_SESSION,
                "agent_id": rt.OTHER_UUID,
            },
        },
        id="use_tool",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "arguments"), REST_CHECK_WORKING_STATE)
async def test_over_rest_check_working_state_refuses_a_caller_bound_as_another_agent(
    monkeypatch, tool_name, arguments
):
    payload, metrics, _resolve = await rt.rest_call(monkeypatch, tool_name, arguments)

    assert _mismatch(payload), payload
    assert metrics.await_count == 0


REST_GET_GOVERNANCE_METRICS = [
    pytest.param(
        "get_governance_metrics",
        {"client_session_id": rt.AGENT_SESSION, "agent_id": rt.OTHER_UUID},
        id="direct",
    ),
    pytest.param(
        "use_tool",
        {
            "tool_name": "get_governance_metrics",
            "arguments": {
                "client_session_id": rt.AGENT_SESSION,
                "agent_id": rt.OTHER_UUID,
            },
        },
        id="use_tool",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "arguments"), REST_GET_GOVERNANCE_METRICS)
async def test_over_rest_get_governance_metrics_reads_the_named_agent_for_a_bound_caller(
    monkeypatch, tool_name, arguments
):
    payload, metrics, _resolve = await rt.rest_call(monkeypatch, tool_name, arguments)

    assert not _mismatch(payload), payload
    assert [call.args[0] for call in metrics.await_args_list] == [rt.OTHER_UUID]
