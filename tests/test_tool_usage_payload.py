"""#1387 — the action discriminator written into ``audit.tool_usage.payload``.

The question the instrument has to answer is "did an agent request a dialectic
review, and who?". Before this change it could not:

  * ``tool_name`` alone collapses ``dialectic(action='request')`` (a review
    request) into the same bucket as ``dialectic(action='list')`` (a dashboard
    read of the sessions table) — 53,825 rows, 41 of them in the last 30 days,
    all indistinguishable.
  * The MCP-protocol wrapper (``src/tool_registration.py``) — the only path a
    Claude Code / claude.ai client takes — recorded nothing at all.
  * The #425 typed identity refusal is deliberately success-SHAPED, so a
    refused call audited as a succeeding anonymous one.

These tests pin all three, plus the two properties that make the payload safe
to ship: the allowlist is enforced in code (no caller value but the clamped
action token can reach the column), and telemetry failure is never fatal.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers  # noqa: F401 — registers every tool definition
from src.services import tool_usage_recorder as recorder
from src.services.tool_usage_recorder import (
    build_tool_usage_payload,
    classify_tool_result,
    resolve_audit_agent_id,
)

AGENT_UUID = "7750bf80-20ad-4108-a952-5271b73845b8"


def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


def _patch_recorder_io(monkeypatch):
    tracker = MagicMock()
    append = AsyncMock(return_value=True)
    monkeypatch.setattr("src.tool_usage_tracker.get_tool_usage_tracker", lambda: tracker)
    monkeypatch.setattr("src.audit_db.append_tool_usage_async", append)
    monkeypatch.setattr("src.background_tasks.create_tracked_task", _consume_coro)
    return tracker, append


# ---------------------------------------------------------------------------
# The discriminator itself
# ---------------------------------------------------------------------------

def test_dialectic_request_is_distinguishable_from_dialectic_list():
    """The whole point: a review REQUEST must not look like a sessions LIST."""
    request = build_tool_usage_payload("dialectic", {"action": "request"})
    listing = build_tool_usage_payload("dialectic", {"action": "list"})

    assert request["action"] == "request"
    assert listing["action"] == "list"
    assert request != listing


def test_router_default_is_not_mistaken_for_an_explicit_call():
    """``dialectic`` defaults to ``list``; the source separates intent from default.

    Without ``action_source`` a bare ``dialectic()`` and an explicit
    ``dialectic(action='list')`` are byte-identical rows, and the kill gate
    cannot subtract router-defaulted calls.
    """
    defaulted = build_tool_usage_payload("dialectic", {})
    explicit = build_tool_usage_payload("dialectic", {"action": "list"})

    assert defaulted == {"action": "list", "action_source": "default"}
    assert explicit == {"action": "list", "action_source": "explicit"}


def test_alias_records_as_typed_and_carries_the_canonical_name():
    """``request_review`` keeps its own tool_name; canonical is recoverable.

    Splitting a merged canonical row back into alias-vs-not is impossible;
    recovering canonical from the payload is a COALESCE. The information loss
    is asymmetric, so the alias stays in ``tool_name``.
    """
    payload = build_tool_usage_payload("request_review", {"issue_description": "x"})

    assert payload["canonical_tool"] == "dialectic"
    assert payload["action"] == "request"
    assert payload["action_source"] == "alias_injected"


def test_op_synonym_and_case_are_normalized():
    assert build_tool_usage_payload("dialectic", {"op": "THESIS"}) == {
        "action": "thesis",
        "action_source": "explicit",
    }


def test_single_purpose_tools_stay_empty():
    """~97% of rows must not grow a payload — get_governance_metrics alone is
    816k rows/week and a sub-action is not a coherent question for it."""
    assert build_tool_usage_payload("get_governance_metrics", {"agent_id": "x"}) is None
    assert build_tool_usage_payload("onboard", {"spawn_reason": "subagent"}) is None
    # A stray `action` kwarg on a single-purpose tool must not invent one.
    assert build_tool_usage_payload("onboard", {"action": "whatever"}) is None


def test_self_recovery_declares_its_vocabulary():
    """Registered by hand (not an action_router) — without default_action a bare
    call audited as no-action while actually running "check"."""
    assert build_tool_usage_payload("self_recovery", {}) == {
        "action": "check",
        "action_source": "default",
    }
    assert build_tool_usage_payload("self_recovery", {"action": "review"})["action"] == "review"


# ---------------------------------------------------------------------------
# Drift guards — a new action must not become silently unauditable
# ---------------------------------------------------------------------------

def test_every_action_router_declares_its_vocabulary():
    """``action_router`` derives ``known_actions`` from its own routing map, so
    this can only fail if the derivation is removed."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    routers = {
        "knowledge", "agent", "calibration", "config", "export",
        "observe", "admin", "dialectic", "research_registry", "self_recovery",
    }
    for name in routers:
        td = _TOOL_DEFINITIONS.get(name)
        assert td is not None, f"{name} is not registered"
        assert td.known_actions, f"{name} declares no known_actions — its rows would carry no discriminator"


def test_every_declared_default_action_is_routable():
    """A ``default_action`` outside ``known_actions`` would audit every
    action-less call as ``action_unlisted``. The decorator refuses it at import
    time; this pins that the live registry is clean."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    for name, td in _TOOL_DEFINITIONS.items():
        if td.default_action and td.known_actions:
            assert td.default_action in td.known_actions, name


def test_declaring_an_unroutable_default_action_is_a_hard_error():
    from src.mcp_handlers.decorators import mcp_tool

    with pytest.raises(ValueError, match="not in known_actions"):
        @mcp_tool(
            "a_tool_with_a_bad_default",
            register=False,
            default_action="nope",
            known_actions={"check"},
        )
        async def _handler(arguments):  # pragma: no cover - never called
            return []


def test_every_known_action_is_classified_by_the_stakes_table():
    """The clamp vocabulary and the #775 classification must describe the same
    surface — a known action that only resolves via the fail-closed default
    means one of the two tables drifted.

    External-plugin tools are exempt for the same reason ``test_stakes_table``
    exempts them: they are deliberately unenumerated and fail closed to "high"
    until an operator classifies them. The exemption set is imported rather
    than restated so the two tests cannot disagree about what "external" means.
    Without this the assertion is order-dependent — ``pi`` only appears once
    some other test in the process has imported ``unitares_pi_plugin``.
    """
    from src.mcp_handlers import stakes_table
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS
    from tests.test_stakes_table import _EXTERNAL_PLUGIN_TOOLS

    unclassified = []
    for name, td in _TOOL_DEFINITIONS.items():
        if name in _EXTERNAL_PLUGIN_TOOLS:
            continue
        module = getattr(td.handler, "__module__", "") or ""
        if not module.startswith("src."):
            continue  # external single-purpose tool — fail-closed-high by design
        for action in td.known_actions or ():
            if (
                stakes_table.get_action_stakes(name, action) == "high"
                and (name, action) not in stakes_table._HIGH
                and (name, None) not in stakes_table._HIGH
            ):
                unclassified.append((name, action))
    assert unclassified == [], (
        f"known actions with no stakes classification: {sorted(unclassified)} — "
        f"add them to stakes_table._HIGH or _BASELINE"
    )


# ---------------------------------------------------------------------------
# The allowlist — enforced in code, not by convention
# ---------------------------------------------------------------------------

_FORBIDDEN = {
    "continuity_token": "hmac-ownership-proof-DO-NOT-LEAK",
    "issue_description": "free text describing private work",
    "reasoning": "more free text",
    "root_cause": "more free text",
    "reflection": "more free text",
    "query": "a search query",
    "proposed_conditions": ["a", "list"],
    "thresholds": {"nested": "structure"},
    "file_path": "/Users/someone/private/path",
    "agent_id": AGENT_UUID,
    "client_session_id": "sess-1",
    "target_agent_id": "another-agent",
    "name": "Some Display Name",
}


@pytest.mark.parametrize("tool_name", ["dialectic", "request_review", "knowledge", "onboard"])
def test_no_non_allowlisted_argument_ever_reaches_the_payload(tool_name):
    arguments = {"action": "request", **_FORBIDDEN}
    payload = build_tool_usage_payload(tool_name, arguments) or {}

    assert set(payload) <= {"action", "canonical_tool", "action_source"}
    flat = " ".join(payload.values())
    for value in _FORBIDDEN.values():
        assert str(value) not in flat
    for key in _FORBIDDEN:
        assert key not in payload


def test_unknown_action_is_clamped_not_echoed():
    """A caller-supplied action outside the routing map must never become a
    GROUP BY key — otherwise ``dialectic(action='<4KB of junk>')`` is a
    cardinality bomb in a 2.5M-row table."""
    payload = build_tool_usage_payload("dialectic", {"action": "A" * 5000})
    assert payload["action"] == "action_unlisted"

    payload = build_tool_usage_payload("knowledge", {"action": "definitely_not_an_action"})
    assert payload["action"] == "action_unlisted"


def test_external_plugin_router_falls_through_to_unlisted():
    """A tool this server does not own (unitares_pi_plugin's ``pi``) has no
    known vocabulary — record that a sub-action existed, not what it said."""
    payload = build_tool_usage_payload("a_tool_this_server_does_not_register", {"action": "zap"})
    assert payload["action"] == "action_unlisted"
    assert "zap" not in str(payload)


def test_sanitizer_drops_hand_built_keys_at_the_write_boundary(monkeypatch):
    """Second enforcement layer: even a call site that hands ``record_tool_usage``
    an arbitrary dict cannot land it in the column."""
    _tracker, append = _patch_recorder_io(monkeypatch)

    recorder.record_tool_usage(
        tool_name="dialectic",
        agent_id=AGENT_UUID,
        success=True,
        payload={
            "action": "request",
            "continuity_token": "SECRET",
            "nested": {"a": 1},
            "numeric": 5,
        },
    )

    written = append.call_args.kwargs["payload"]
    assert written == {"action": "request"}


# ---------------------------------------------------------------------------
# Build order: BEFORE dispatch, because the pipeline mutates `arguments`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payload_is_built_before_alias_injection_mutates_arguments():
    """``params_step.resolve_alias`` writes ``arguments['action'] = 'request'``
    into the CALLER'S dict — ``run_tool_dispatch_pipeline`` makes no copy. A
    payload built after dispatch would call every ``request_review``
    ``action_source='explicit'`` and destroy the alias-adoption signal.
    """
    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.params_step import resolve_alias

    arguments = {"issue_description": "x"}
    before = build_tool_usage_payload("request_review", arguments)

    await resolve_alias("request_review", arguments, DispatchContext())
    assert arguments["action"] == "request"  # the in-place mutation, pinned

    after = build_tool_usage_payload("request_review", arguments)

    assert before["action_source"] == "alias_injected"
    assert after["action_source"] == "explicit"  # what the wrong order records
    assert before != after


# ---------------------------------------------------------------------------
# End-to-end: the payload reaches the DB writer on every surface
# ---------------------------------------------------------------------------

async def _dispatch_that_injects_the_alias_action(_name, arguments):
    """Stand-in for the real pipeline's in-place mutation.

    ``params_step.resolve_alias`` does exactly this to the CALLER'S dict
    (``run_tool_dispatch_pipeline`` makes no defensive copy). Reproducing it
    here is what makes the surface tests below regression tests for the
    BUILD ORDER: if a call site moves its ``build_tool_usage_payload`` after
    dispatch, ``action_source`` flips to "explicit" and they fail.
    """
    if isinstance(arguments, dict) and "action" not in arguments:
        arguments["action"] = "request"
    return {"success": True}


@pytest.mark.asyncio
async def test_rest_surface_writes_the_payload(monkeypatch):
    from src.services.http_tool_service import execute_http_tool

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(
        "src.services.http_tool_service.get_direct_http_tool_handler", lambda _n: None
    )
    monkeypatch.setattr(
        "src.services.http_tool_service.execute_http_dispatch_fallback",
        _dispatch_that_injects_the_alias_action,
    )

    arguments = {"issue_description": "x"}
    await execute_http_tool("request_review", arguments)

    assert arguments["action"] == "request"  # dispatch really did mutate it
    assert append.call_args.kwargs["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_rest_refusal_path_writes_a_countable_row(monkeypatch):
    """A call that fails auth must be countable, not invisible — and must carry
    the discriminator so "refused review requests" is answerable."""
    from src.services import http_tool_service

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(
        http_tool_service,
        "_strict_identity_refusal_or_none",
        lambda _t, _a: {"status": "identity_required"},
    )

    await http_tool_service.execute_http_tool("request_review", {"issue_description": "x"})

    kwargs = append.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error_type"] == "identity_required"
    assert kwargs["payload"]["action"] == "request"


@pytest.mark.asyncio
async def test_stdio_surface_writes_the_payload(monkeypatch):
    import src.mcp_server_std as std

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(std, "STDIO_PROXY_HTTP_URL", None, raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_URL", None, raising=False)
    monkeypatch.setattr(
        "src.mcp_handlers.dispatch_tool", _dispatch_that_injects_the_alias_action
    )

    arguments = {"issue_description": "x"}
    await std.call_tool("request_review", arguments)

    assert arguments["action"] == "request"  # dispatch really did mutate it
    assert append.call_args.kwargs["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_mcp_protocol_wrapper_records_at_all(monkeypatch):
    """The MCP-protocol surface (/mcp, /sse) wrote NOTHING before #1387."""
    import src.tool_registration as tr

    tr._tool_wrappers_cache.clear()
    calls = []
    monkeypatch.setattr(tr, "record_tool_usage", lambda **kw: calls.append(kw))
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(tr, "dispatch_tool", _dispatch_that_injects_the_alias_action)

    try:
        await tr.get_tool_wrapper("request_review")(issue_description="x")
    finally:
        tr._tool_wrappers_cache.clear()

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "request_review"
    assert calls[0]["success"] is True
    assert calls[0]["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_mcp_protocol_wrapper_counts_the_identity_refusal(monkeypatch):
    """The #425 refusal is success-shaped; it must still audit as a failure."""
    import src.tool_registration as tr
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    tr._tool_wrappers_cache.clear()
    calls = []
    monkeypatch.setattr(tr, "record_tool_usage", lambda **kw: calls.append(kw))
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(
        tr,
        "dispatch_tool",
        AsyncMock(return_value=success_response(strict_identity_refusal_payload("dialectic"))),
    )

    try:
        await tr.get_tool_wrapper("request_review")(issue_description="x")
    finally:
        tr._tool_wrappers_cache.clear()

    assert len(calls) == 1
    assert calls[0]["success"] is False
    assert calls[0]["error_type"] == "identity_required"


# ---------------------------------------------------------------------------
# Refusal classification + attribution
# ---------------------------------------------------------------------------

def test_typed_identity_refusal_classifies_as_failure():
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload

    payload = {"success": True, **strict_identity_refusal_payload("dialectic")}
    assert classify_tool_result(payload) == (False, "identity_required")

    lineage = {
        "success": True,
        **strict_identity_refusal_payload("onboard", status="lineage_declaration_required"),
    }
    assert classify_tool_result(lineage) == (False, "lineage_declaration_required")


def test_governance_verdicts_are_still_successes():
    """A pause/reject verdict is a SUCCESSFUL call — the refusal detector must
    not widen into governance outcomes (that circularity is why
    ``state_error`` is excluded)."""
    assert classify_tool_result({"success": True, "verdict": "pause"}) == (True, None)
    assert classify_tool_result(
        {"success": False, "error_category": "state_error"}
    ) == (True, None)


def test_attribution_falls_back_to_the_resolved_binding(monkeypatch):
    """``identity`` (3,049 rows/30d) and every ``pre_onboard`` read audited as
    anonymous because the recorder only ever read ``arguments['agent_id']``."""
    monkeypatch.setattr(
        "src.mcp_handlers.context.get_context_agent_id", lambda: AGENT_UUID
    )
    assert resolve_audit_agent_id(None) == AGENT_UUID
    # Request-side always wins.
    assert resolve_audit_agent_id("explicit-value") == "explicit-value"


def test_attribution_fallback_admits_only_uuids(monkeypatch):
    """``set_session_context`` seeds the contextvar from the UNVERIFIED
    ``X-Agent-Id`` header before any resolution runs. The fallback may only
    ever add a joinable UUID, never an arbitrary caller string."""
    monkeypatch.setattr(
        "src.mcp_handlers.context.get_context_agent_id", lambda: "Claude_Code_20260727"
    )
    assert resolve_audit_agent_id(None) is None


def test_presence_refresh_still_keys_on_the_request_side_id(monkeypatch):
    """The agent:/ presence lease is a liveness claim on another surface —
    the audit-only attribution fallback must not widen who refreshes it."""
    _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(agent_id),
    )
    monkeypatch.setattr(
        "src.mcp_handlers.context.get_context_agent_id", lambda: AGENT_UUID
    )

    recorder.record_tool_usage(tool_name="knowledge", agent_id=None, success=True)

    assert scheduled == []


# ---------------------------------------------------------------------------
# Telemetry failure is never fatal
# ---------------------------------------------------------------------------

def test_payload_build_failure_is_non_fatal():
    with patch(
        "src.mcp_handlers.decorators.resolve_canonical_action_and_source",
        side_effect=RuntimeError("resolver exploded"),
    ):
        assert build_tool_usage_payload("dialectic", {"action": "request"}) is None


def test_recorder_survives_every_sink_failing(monkeypatch):
    monkeypatch.setattr(
        "src.tool_usage_tracker.get_tool_usage_tracker",
        MagicMock(side_effect=RuntimeError("jsonl down")),
    )
    # Plain MagicMock (not AsyncMock): returns a sentinel rather than a live
    # coroutine, so the raising create_tracked_task below cannot orphan one.
    monkeypatch.setattr("src.audit_db.append_tool_usage_async", MagicMock())
    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    recorder.record_tool_usage(
        tool_name="dialectic",
        agent_id=AGENT_UUID,
        success=True,
        payload={"action": "request"},
    )


@pytest.mark.asyncio
async def test_wrapper_survives_a_broken_recorder(monkeypatch):
    """A telemetry explosion must never break, or change, a tool call."""
    import json as _json

    from mcp.types import TextContent

    import src.tool_registration as tr

    handler_response = [
        TextContent(type="text", text=_json.dumps({"success": True, "sessions": []}))
    ]

    tr._tool_wrappers_cache.clear()
    monkeypatch.setattr(
        tr, "record_tool_usage", MagicMock(side_effect=RuntimeError("telemetry down"))
    )
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(tr, "dispatch_tool", AsyncMock(return_value=handler_response))

    try:
        result = await tr.get_tool_wrapper("dialectic")(action="list")
    finally:
        tr._tool_wrappers_cache.clear()

    # The caller must still get the HANDLER'S response, not the wrapper's
    # error envelope — telemetry may not change what a tool returns.
    assert result == {"success": True, "sessions": []}
