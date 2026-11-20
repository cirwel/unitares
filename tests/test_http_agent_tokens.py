"""Per-agent REST tokens admit one agent's check-ins and nothing else.

See src/http_agent_tokens.py. The token authorizes; it never binds. Every test
below drives the real /v1/tools/call route with a public source address, so
no trusted-network bypass applies, and stubs only caller resolution and the
tool itself.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from src import http_agent_tokens

AGENT = "69a1a4f7-0000-4000-8000-000000000001"
OTHER = "99999999-9999-4999-8999-999999999999"
TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64


def _entry(agent: str, token: str, scopes: str | None = None) -> str:
    base = f"{agent}:sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    return f"{base}:{scopes}" if scopes is not None else base


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "UNITARES_MCP_BEARER_TOKENS",
        "UNITARES_HTTP_API_TOKEN",
        "UNITARES_REST_STRICT",
        "UNITARES_TRUSTED_NETWORKS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(http_agent_tokens.AGENT_TOKENS_ENV, _entry(AGENT, TOKEN))
    yield


# --- the allowlist -------------------------------------------------------------


def test_bearer_maps_to_its_agent():
    assert http_agent_tokens.agent_for_bearer(TOKEN) == AGENT


def test_unknown_bearer_maps_to_nothing():
    assert http_agent_tokens.agent_for_bearer(OTHER_TOKEN) is None
    assert http_agent_tokens.agent_for_bearer(None) is None
    assert http_agent_tokens.agent_for_bearer("") is None


def test_unset_allowlist_admits_nothing(monkeypatch):
    monkeypatch.delenv(http_agent_tokens.AGENT_TOKENS_ENV)
    assert http_agent_tokens.agent_for_bearer(TOKEN) is None


def test_two_agents_each_get_their_own(monkeypatch):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        f"{_entry(AGENT, TOKEN)}, {_entry(OTHER, OTHER_TOKEN)}",
    )
    assert http_agent_tokens.agent_for_bearer(TOKEN) == AGENT
    assert http_agent_tokens.agent_for_bearer(OTHER_TOKEN) == OTHER


def test_uppercase_digest_and_uuid_are_normalized(monkeypatch):
    digest = hashlib.sha256(TOKEN.encode()).hexdigest().upper()
    monkeypatch.setenv(http_agent_tokens.AGENT_TOKENS_ENV, f"{AGENT.upper()}:sha256:{digest}")
    assert http_agent_tokens.agent_for_bearer(TOKEN) == AGENT


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-uuid:sha256:" + "0" * 64,
        f"{AGENT}:md5:" + "0" * 64,
        f"{AGENT}:sha256:" + "0" * 63,
        f"{AGENT}:sha256:" + "z" * 64,
        TOKEN,  # a raw token pasted where a digest belongs
    ],
)
def test_a_malformed_entry_disables_every_agent_token(monkeypatch, raw):
    # Fail closed: the good entry next to the bad one is refused too, so a
    # typo is noticed instead of half-working.
    monkeypatch.setenv(http_agent_tokens.AGENT_TOKENS_ENV, f"{_entry(AGENT, TOKEN)},{raw}")
    assert http_agent_tokens.agent_for_bearer(TOKEN) is None


def test_a_repeated_digest_is_malformed(monkeypatch):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV, f"{_entry(AGENT, TOKEN)},{_entry(OTHER, TOKEN)}"
    )
    assert http_agent_tokens.agent_for_bearer(TOKEN) is None


def test_other_rest_routes_do_not_accept_an_agent_token():
    from src.http_routes.access import _check_http_auth

    request = _plain_request(auth=f"Bearer {TOKEN}")
    assert _check_http_auth(request, http_api_token=None) is False


# --- the route -----------------------------------------------------------------


class _plain_request:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth else {}
        self.client = type("C", (), {"host": "203.0.113.7"})()
        self.method = "POST"
        self.scope = {}
        self.state = type("State", (), {})()


def _request(name, arguments, *, auth=f"Bearer {TOKEN}", extra_headers=()):
    from starlette.requests import Request

    body = json.dumps({"name": name, "arguments": arguments}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if auth:
        headers.append((b"authorization", auth.encode()))
    headers.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/tools/call",
        "raw_path": b"/v1/tools/call",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.7", 43210),
        "server": ("127.0.0.1", 8767),
        "state": {"_http_api_mcp_server_name": "unitares-governance"},
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


@pytest.fixture
def route(monkeypatch):
    """The real route; caller resolution binds ``resolves_to`` and the tool is a mock."""
    from src.http_routes import tools
    from src.mcp_handlers.context import update_context_agent_id

    state = {"resolves_to": AGENT, "prebinds": 0}

    async def resolve(tool_name, arguments, signals):
        state["prebinds"] += 1
        if state["resolves_to"]:
            update_context_agent_id(state["resolves_to"])
        return state["resolves_to"]

    execute = AsyncMock(return_value={"action": "proceed"})
    monkeypatch.setattr(tools.access, "_resolve_http_bound_agent", resolve)
    monkeypatch.setattr(tools, "execute_http_tool", execute)
    state["execute"] = execute
    state["call"] = tools.http_call_tool
    return state


CHECKIN = {"client_session_id": "agent-69a1a4f7-000", "response_mode": "minimal"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["process_agent_update", "sync_state"])
async def test_own_checkin_is_admitted(route, name):
    response = await route["call"](_request(name, dict(CHECKIN)))
    assert response.status_code == 200
    route["execute"].assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_token_is_unauthorized(route):
    response = await route["call"](_request("process_agent_update", dict(CHECKIN), auth=f"Bearer {OTHER_TOKEN}"))
    assert response.status_code == 401
    assert route["prebinds"] == 0


@pytest.mark.asyncio
async def test_no_token_is_unauthorized(route):
    response = await route["call"](_request("process_agent_update", dict(CHECKIN), auth=None))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_call_resolving_to_another_agent_is_refused(route):
    route["resolves_to"] = OTHER
    response = await route["call"](_request("process_agent_update", dict(CHECKIN)))
    assert response.status_code == 403
    assert json.loads(response.body)["error_type"] == "AgentTokenScope"
    route["execute"].assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unresolved_call_is_refused_with_the_operator_recovery(route):
    route["resolves_to"] = None
    response = await route["call"](_request("process_agent_update", dict(CHECKIN)))
    assert response.status_code == 403
    body = json.loads(response.body)
    assert body["error_type"] == "AgentTokenUnbound"
    assert "rebind-resident-session.sh" in body["recovery"]
    route["execute"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["identity", "onboard", "start_session", "knowledge", "outcome_event", "record_result"]
)
async def test_other_tools_are_refused_before_resolution(route, name):
    response = await route["call"](_request(name, dict(CHECKIN)))
    assert response.status_code == 403
    assert route["prebinds"] == 0
    route["execute"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["agent_id", "agent_uuid", "continuity_token"])
@pytest.mark.parametrize("wrapping", ["top", "dict", "json", "double"])
async def test_identity_arguments_are_refused_at_every_depth(route, key, wrapping):
    inner = {key: OTHER}
    if wrapping == "top":
        arguments = {**CHECKIN, **inner}
    elif wrapping == "dict":
        arguments = {**CHECKIN, "kwargs": inner}
    elif wrapping == "json":
        arguments = {**CHECKIN, "kwargs": json.dumps(inner)}
    else:
        arguments = {**CHECKIN, "kwargs": json.dumps({"kwargs": json.dumps(inner)})}
    response = await route["call"](_request("process_agent_update", arguments))
    assert response.status_code == 403
    assert route["prebinds"] == 0


@pytest.mark.asyncio
async def test_an_operator_header_is_refused(route):
    response = await route["call"](
        _request(
            "process_agent_update",
            dict(CHECKIN),
            extra_headers=[(b"x-unitares-operator", b"anything")],
        )
    )
    assert response.status_code == 403
    assert route["prebinds"] == 0


@pytest.mark.asyncio
async def test_strict_posture_admits_an_agent_token(route, monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-bearer")
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    response = await route["call"](_request("process_agent_update", dict(CHECKIN)))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_shared_token_callers_are_not_scoped(route, monkeypatch):
    # The shared token keeps its full reach; agent scoping applies only when
    # the agent token is what admitted the request.
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "shared")
    route["resolves_to"] = OTHER
    response = await route["call"](_request("identity", dict(CHECKIN), auth="Bearer shared"))
    assert response.status_code == 200


# --- per-token scopes and argument allowlists ---------------------------------


@pytest.mark.parametrize(
    "raw_scopes",
    ["", "unknown", "checkin+", "+outcome", "checkin+checkin", "admin"],
)
def test_malformed_scope_suffix_fails_closed(monkeypatch, raw_scopes):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        f"{_entry(AGENT, TOKEN)},{_entry(OTHER, OTHER_TOKEN, raw_scopes)}",
    )
    assert http_agent_tokens.grant_for_bearer(TOKEN) is None
    assert http_agent_tokens.grant_for_bearer(OTHER_TOKEN) is None


def test_two_tokens_same_agent_have_independent_grants_and_revocation(monkeypatch):
    second_token = "c" * 64
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        f"{_entry(AGENT, TOKEN)},{_entry(AGENT, second_token, 'checkin+outcome+knowledge_store')}",
    )
    grant1 = http_agent_tokens.grant_for_bearer(TOKEN)
    grant2 = http_agent_tokens.grant_for_bearer(second_token)
    assert grant1 is not None and grant2 is not None
    assert grant1.agent_uuid == AGENT and grant2.agent_uuid == AGENT
    assert grant1.scopes == frozenset({"checkin"})
    assert grant2.scopes == frozenset({"checkin", "outcome", "knowledge_store"})
    assert grant1.digest_prefix != grant2.digest_prefix

    # Revoking one leaves the other working with its own grant
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        _entry(AGENT, second_token, "checkin+outcome+knowledge_store"),
    )
    assert http_agent_tokens.grant_for_bearer(TOKEN) is None
    assert http_agent_tokens.grant_for_bearer(second_token) == grant2


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["kwargs", "arguments", "params"])
async def test_wrapper_keys_are_refused(route, monkeypatch, key):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        _entry(AGENT, TOKEN, "checkin+outcome+knowledge_store"),
    )
    response = await route["call"](
        _request("process_agent_update", {**CHECKIN, key: {"complexity": 0.2}})
    )
    assert response.status_code == 403
    assert route["prebinds"] == 0


@pytest.mark.asyncio
async def test_x_agent_id_header_and_missing_csid_are_refused(route):
    res_hdr = await route["call"](
        _request(
            "process_agent_update",
            dict(CHECKIN),
            extra_headers=[(b"x-agent-id", AGENT.encode())],
        )
    )
    assert res_hdr.status_code == 403
    assert route["prebinds"] == 0

    for bad_args in ({}, {"client_session_id": ""}, {"client_session_id": "   "}):
        res_csid = await route["call"](_request("process_agent_update", bad_args))
        assert res_csid.status_code == 403
        assert route["prebinds"] == 0


@pytest.mark.asyncio
async def test_outcome_scope_admits_allowed_fields_and_blocks_session_or_prediction_injection(
    route, monkeypatch
):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        _entry(AGENT, TOKEN, "outcome"),
    )
    # Checkin fails without checkin scope
    res_checkin = await route["call"](_request("sync_state", dict(CHECKIN)))
    assert res_checkin.status_code == 403
    assert route["prebinds"] == 0

    for tool_name in ("outcome_event", "record_result"):
        res_ok = await route["call"](
            _request(
                tool_name,
                {
                    "client_session_id": CHECKIN["client_session_id"],
                    "outcome_type": "task_completed",
                    "outcome_score": 0.9,
                    "detail": {"mark_count": 42},
                },
            )
        )
        assert res_ok.status_code == 200

    route["execute"].reset_mock()
    for bad_field in (
        {"session_id": "other-session"},
        {"prediction_id": "pred-123"},
        {"verification_source": "external_signal"},
        {"_trusted_ingestion": True},
        {"agent_id": OTHER},
        {"unexpected": 1},
    ):
        prebinds_before = route["prebinds"]
        res_bad = await route["call"](
            _request(
                "outcome_event",
                {
                    "client_session_id": CHECKIN["client_session_id"],
                    "outcome_type": "task_completed",
                    **bad_field,
                },
            )
        )
        assert res_bad.status_code == 403
        assert route["prebinds"] == prebinds_before
    route["execute"].assert_not_awaited()


@pytest.mark.asyncio
async def test_knowledge_store_scope_is_append_only_and_low_medium_severity(
    route, monkeypatch
):
    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        _entry(AGENT, TOKEN, "knowledge_store"),
    )

    # Canonical explicit action="store" and store_finding alias succeed
    res_canon = await route["call"](
        _request(
            "knowledge",
            {
                "client_session_id": CHECKIN["client_session_id"],
                "action": "store",
                "summary": "Room warmed rapidly after sunrise",
                "discovery_type": "insight",
                "tags": ["embodied", "light"],
                "severity": "low",
                "details": "{}",
            },
        )
    )
    assert res_canon.status_code == 200

    res_alias = await route["call"](
        _request(
            "store_finding",
            {
                "client_session_id": CHECKIN["client_session_id"],
                "summary": "Ambient lux correlated with clarity",
                "discovery_type": "observation",
                "severity": "medium",
            },
        )
    )
    assert res_alias.status_code == 200

    route["execute"].reset_mock()
    bad_calls = [
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "summary": "missing action"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "search", "summary": "x"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "update", "summary": "x"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "cleanup", "summary": "x"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "STORE", "summary": "x"}),
        ("store_finding", {"client_session_id": CHECKIN["client_session_id"], "action": "delete", "summary": "x"}),
        ("update_finding", {"client_session_id": CHECKIN["client_session_id"], "summary": "x"}),
        ("search_shared_memory", {"client_session_id": CHECKIN["client_session_id"], "summary": "x"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "severity": "high"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "severity": "critical"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "severity": "LOW"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "architectural_decision"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "learning"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "pattern"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "root_cause_analysis"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "migration"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_type": "topic_rollup"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discoveries": []}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "response_to": {"discovery_id": "1"}}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "supersedes": "d-1"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "discovery_id": "custom"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "status": "resolved"}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["permanent"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["#permanent"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["permanent!"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["foundational"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["Foundational."]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["architecture"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": [" Decision "]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["-decision-"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["source-claude-memory"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["source_claude_memory"]}),
        ("knowledge", {"client_session_id": CHECKIN["client_session_id"], "action": "store", "summary": "x", "tags": ["Source Claude Memory"]}),
    ]
    for tool_name, args in bad_calls:
        prebinds_before = route["prebinds"]
        res_bad = await route["call"](_request(tool_name, args))
        assert res_bad.status_code == 403, f"expected 403 for {tool_name} {args}"
        assert route["prebinds"] == prebinds_before
    route["execute"].assert_not_awaited()

    from src.knowledge_graph_lifecycle import PERMANENT_TAGS, PERMANENT_TYPES

    assert http_agent_tokens._KNOWLEDGE_STORE_ALLOWED_TYPES.isdisjoint(PERMANENT_TYPES)
    assert {t.lower() for t in PERMANENT_TAGS} <= http_agent_tokens._KNOWLEDGE_STORE_RESERVED_TAGS


@pytest.mark.asyncio
async def test_all_registered_aliases_fail_closed_unless_explicitly_scoped(route, monkeypatch):
    from src.mcp_handlers.tool_stability import list_all_aliases

    monkeypatch.setenv(
        http_agent_tokens.AGENT_TOKENS_ENV,
        _entry(AGENT, TOKEN, "checkin+outcome+knowledge_store"),
    )
    allowed_aliases = {"sync_state", "record_result", "store_finding"}
    for alias_name in list_all_aliases():
        args = {"client_session_id": CHECKIN["client_session_id"]}
        if alias_name == "outcome_event" or alias_name == "record_result":
            args["outcome_type"] = "task_completed"
        elif alias_name == "store_finding":
            args["summary"] = "test"
        prebinds_before = route["prebinds"]
        res = await route["call"](_request(alias_name, args))
        if alias_name in allowed_aliases:
            assert res.status_code == 200, f"expected 200 for {alias_name}"
        else:
            assert res.status_code == 403, f"expected 403 for {alias_name}"
            assert route["prebinds"] == prebinds_before
